"""In-memory coordination of supplied research mission results and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from federation.capability import NodeCapability
from research_mission.models import (
    AuthorizationLevel,
    ResearchMission,
    ResearchMissionPlan,
    ResearchMissionStatus,
    ResearchRole,
    ResearchTaskSpec,
)
from research_mission.results import (
    ResearchEvidence,
    ResearchTaskResult,
    ResearchTaskResultStatus,
    _copy_evidence,
    _copy_result,
    _identifier,
    _result_identity,
)


class ResearchResultConflictError(ValueError):
    """Raised when one planned task receives conflicting terminal results."""


class ResearchEvidenceConflictError(ValueError):
    """Raised when an evidence identity is submitted more than once."""


class ResearchResultPrerequisiteError(ValueError):
    """Raised when a result is submitted before required completed results."""


class ResearchMissionContractError(RuntimeError):
    """Raised when the mission identity, plan, or lifecycle contract changes."""


def _copy_capabilities(
    values: frozenset[NodeCapability],
    field_name: str,
) -> frozenset[NodeCapability]:
    if type(values) is not frozenset:
        raise TypeError(f"{field_name} must be a frozenset")
    copies = []
    for capability in values:
        if type(capability) is not NodeCapability:
            raise TypeError(f"{field_name} must contain NodeCapability values")
        name = _identifier(capability.name, f"{field_name} entry")
        copied = NodeCapability(name)
        if copied.name != name:
            raise ValueError(f"{field_name} capability names must be canonical")
        copies.append(copied)
    result = frozenset(copies)
    if len(result) != len(values):
        raise ValueError(f"{field_name} capability names must be unique")
    return result


def _task_contract_identity(task: ResearchTaskSpec):
    if type(task) is not ResearchTaskSpec:
        raise TypeError("mission plan tasks must be ResearchTaskSpec values")
    task_id = _identifier(task.task_id, "task_id")
    mission_id = _identifier(task.mission_id, "mission_id")
    objective = _identifier(task.objective, "objective")
    expected_result = _identifier(task.expected_result, "expected_result")
    if type(task.role) is not ResearchRole:
        raise TypeError("mission plan task role must be a ResearchRole")
    if type(task.sequence) is not int or task.sequence < 1:
        raise TypeError("mission plan task sequence must be a positive integer")
    if not isinstance(task.research_context, dict):
        raise TypeError("mission plan task research_context must be a dict")
    if type(task.authorization_level) is not AuthorizationLevel:
        raise TypeError("mission plan task authorization_level is invalid")
    if type(task.approval_required) is not bool:
        raise TypeError("mission plan task approval_required must be a boolean")
    required = _copy_capabilities(
        task.required_capabilities,
        "required_capabilities",
    )
    preferred = _copy_capabilities(
        task.preferred_capabilities,
        "preferred_capabilities",
    )
    if required & preferred:
        raise ValueError("required and preferred capabilities must not overlap")
    if type(task.depends_on) is not tuple:
        raise TypeError("mission plan task depends_on must be a tuple")
    dependencies = tuple(
        _identifier(dependency, "depends_on entry")
        for dependency in task.depends_on
    )
    return (
        task_id,
        mission_id,
        task.role.value,
        task.sequence,
        objective,
        expected_result,
        dependencies,
        task.authorization_level.value,
        task.approval_required,
        tuple(sorted(capability.name for capability in required)),
        tuple(sorted(capability.name for capability in preferred)),
    )


def _copy_task_spec(task: ResearchTaskSpec) -> ResearchTaskSpec:
    _task_contract_identity(task)
    return ResearchTaskSpec(
        task_id=task.task_id,
        mission_id=task.mission_id,
        role=task.role,
        sequence=task.sequence,
        objective=task.objective,
        research_context=task.research_context,
        expected_result=task.expected_result,
        authorization_level=task.authorization_level,
        approval_required=task.approval_required,
        required_capabilities=_copy_capabilities(
            task.required_capabilities,
            "required_capabilities",
        ),
        preferred_capabilities=_copy_capabilities(
            task.preferred_capabilities,
            "preferred_capabilities",
        ),
        depends_on=task.depends_on,
    )


def _plan_contract_identity(plan: ResearchMissionPlan):
    if type(plan) is not ResearchMissionPlan:
        raise TypeError("mission plan must be a ResearchMissionPlan")
    mission_id = _identifier(plan.mission_id, "plan mission_id")
    if type(plan.tasks) is not tuple or not plan.tasks:
        raise TypeError("mission plan tasks must be a non-empty tuple")
    return (
        mission_id,
        tuple(_task_contract_identity(task) for task in plan.tasks),
    )


@dataclass(slots=True, frozen=True)
class ResearchMissionResultState:
    """Deterministic immutable inspection snapshot for one mission."""

    mission_id: str
    results: tuple[ResearchTaskResult, ...]
    completed_results: tuple[ResearchTaskResult, ...]
    failed_results: tuple[ResearchTaskResult, ...]
    blocked_results: tuple[ResearchTaskResult, ...]
    pending_tasks: tuple[ResearchTaskSpec, ...]
    unsatisfied_tasks: tuple[ResearchTaskSpec, ...]
    evidence: tuple[ResearchEvidence, ...]
    results_sufficient: bool
    completion_eligible: bool


class ResearchMissionResultCoordinator:
    """Validate and inspect supplied results without executing research work."""

    def __init__(self, mission: ResearchMission):
        if type(mission) is not ResearchMission:
            raise TypeError("mission must be a ResearchMission")
        if type(mission.plan) is not ResearchMissionPlan:
            raise ValueError("mission must have an existing planned ResearchMissionPlan")
        mission_id = _identifier(mission.mission_id, "mission_id")
        plan_mission_id = _identifier(mission.plan.mission_id, "plan mission_id")
        if plan_mission_id != mission_id:
            raise ValueError("mission plan must match the mission identity")
        source_plan_identity = _plan_contract_identity(mission.plan)

        self._mission = mission
        self._source_plan = mission.plan
        self._tasks = tuple(_copy_task_spec(task) for task in mission.plan.tasks)
        self._plan = ResearchMissionPlan(
            mission_id=plan_mission_id,
            tasks=self._tasks,
        )
        self._plan_identity = _plan_contract_identity(self._plan)
        if source_plan_identity != self._plan_identity:
            raise ValueError("mission plan could not be captured without normalization")
        self._tasks_by_id = {task.task_id: task for task in self._tasks}
        self._sequence_by_task_id = {
            task.task_id: task.sequence for task in self._tasks
        }
        self._results_by_task_id: dict[str, ResearchTaskResult] = {}
        self._evidence_by_id: dict[str, ResearchEvidence] = {}
        self._lock = RLock()
        self._require_mission_contract_locked()

    @property
    def mission_id(self) -> str:
        return self._plan.mission_id

    def register_result(
        self,
        result: ResearchTaskResult,
        *,
        evidence=(),
    ) -> ResearchTaskResult:
        """Register one terminal result and an optional atomic evidence batch."""

        candidate = _copy_result(result)
        evidence_candidates = self._copy_evidence_batch(evidence)

        with self._lock:
            self._require_mission_contract_locked()
            task = self._validate_result_attribution(candidate)
            if not self._prerequisites_satisfied_locked(task):
                raise ResearchResultPrerequisiteError(
                    f"task {task.task_id!r} has incomplete result prerequisites"
                )

            existing = self._results_by_task_id.get(task.task_id)
            if existing is not None and (
                _result_identity(existing) != _result_identity(candidate)
            ):
                raise ResearchResultConflictError(
                    f"conflicting result for planned task {task.task_id!r}"
                )

            self._validate_evidence_batch_locked(
                evidence_candidates,
                expected_task_id=task.task_id,
                allow_unregistered_task_id=(
                    task.task_id if existing is None else None
                ),
            )
            self._require_mission_contract_locked()

            stored = existing or candidate
            if existing is None:
                self._results_by_task_id[task.task_id] = candidate
            for item in evidence_candidates:
                self._evidence_by_id[item.evidence_id] = item
            return _copy_result(stored)

    def attach_evidence(self, evidence: ResearchEvidence) -> ResearchEvidence:
        """Attach one supplied evidence item to its exact registered task result."""

        return self.attach_evidence_batch((evidence,))[0]

    def attach_evidence_batch(self, evidence) -> tuple[ResearchEvidence, ...]:
        """Attach an evidence batch atomically after validating every item."""

        candidates = self._copy_evidence_batch(evidence)
        with self._lock:
            self._require_mission_contract_locked()
            self._validate_evidence_batch_locked(candidates)
            self._require_mission_contract_locked()
            for item in candidates:
                self._evidence_by_id[item.evidence_id] = item
            return self._copy_evidence_items(candidates)

    def get_result(self, task_id: str) -> ResearchTaskResult | None:
        task = self._planned_task(task_id)
        with self._lock:
            self._require_mission_contract_locked()
            result = self._results_by_task_id.get(task.task_id)
            return None if result is None else _copy_result(result)

    def results(self) -> tuple[ResearchTaskResult, ...]:
        with self._lock:
            self._require_mission_contract_locked()
            return self._copy_results(self._results_locked())

    def completed_results(self) -> tuple[ResearchTaskResult, ...]:
        with self._lock:
            self._require_mission_contract_locked()
            return self._copy_results(
                self._results_with_status_locked(ResearchTaskResultStatus.COMPLETED)
            )

    def failed_results(self) -> tuple[ResearchTaskResult, ...]:
        with self._lock:
            self._require_mission_contract_locked()
            return self._copy_results(
                self._results_with_status_locked(ResearchTaskResultStatus.FAILED)
            )

    def blocked_results(self) -> tuple[ResearchTaskResult, ...]:
        with self._lock:
            self._require_mission_contract_locked()
            return self._copy_results(
                self._results_with_status_locked(ResearchTaskResultStatus.BLOCKED)
            )

    def pending_tasks(self) -> tuple[ResearchTaskSpec, ...]:
        """Return planned tasks that have not reported any terminal result."""

        with self._lock:
            self._require_mission_contract_locked()
            pending = tuple(
                task
                for task in self._tasks
                if task.task_id not in self._results_by_task_id
            )
            return self._copy_tasks(pending)

    def unsatisfied_tasks(self) -> tuple[ResearchTaskSpec, ...]:
        """Return tasks without an acceptable COMPLETED result."""

        with self._lock:
            self._require_mission_contract_locked()
            return self._copy_tasks(self._unsatisfied_tasks_locked())

    def evidence(self) -> tuple[ResearchEvidence, ...]:
        with self._lock:
            self._require_mission_contract_locked()
            return self._copy_evidence_items(self._evidence_locked())

    def evidence_for_task(self, task_id: str) -> tuple[ResearchEvidence, ...]:
        task = self._planned_task(task_id)
        with self._lock:
            self._require_mission_contract_locked()
            items = tuple(
                item
                for item in self._evidence_locked()
                if item.task_id == task.task_id
            )
            return self._copy_evidence_items(items)

    def prerequisites_satisfied(self, task_id: str) -> bool:
        task = self._planned_task(task_id)
        with self._lock:
            self._require_mission_contract_locked()
            return self._prerequisites_satisfied_locked(task)

    def has_sufficient_results(self) -> bool:
        with self._lock:
            self._require_mission_contract_locked()
            return not self._unsatisfied_tasks_locked()

    def is_completion_eligible(self) -> bool:
        """Return eligibility only; never mutate the mission lifecycle."""

        with self._lock:
            self._require_mission_contract_locked()
            return self._completion_eligible_locked()

    def inspect(self) -> ResearchMissionResultState:
        with self._lock:
            self._require_mission_contract_locked()
            results = self._results_locked()
            completed = tuple(
                result
                for result in results
                if result.status is ResearchTaskResultStatus.COMPLETED
            )
            failed = tuple(
                result
                for result in results
                if result.status is ResearchTaskResultStatus.FAILED
            )
            blocked = tuple(
                result
                for result in results
                if result.status is ResearchTaskResultStatus.BLOCKED
            )
            pending = tuple(
                task
                for task in self._tasks
                if task.task_id not in self._results_by_task_id
            )
            unsatisfied = self._unsatisfied_tasks_locked()
            sufficient = not unsatisfied
            return ResearchMissionResultState(
                mission_id=self.mission_id,
                results=self._copy_results(results),
                completed_results=self._copy_results(completed),
                failed_results=self._copy_results(failed),
                blocked_results=self._copy_results(blocked),
                pending_tasks=self._copy_tasks(pending),
                unsatisfied_tasks=self._copy_tasks(unsatisfied),
                evidence=self._copy_evidence_items(self._evidence_locked()),
                results_sufficient=sufficient,
                completion_eligible=(
                    sufficient
                    and (
                        self._mission.status is ResearchMissionStatus.ROUTED
                        or self._mission.status is ResearchMissionStatus.DISPATCHED
                    )
                ),
            )

    def _planned_task(self, task_id: str) -> ResearchTaskSpec:
        task_id = _identifier(task_id, "task_id")
        try:
            return self._tasks_by_id[task_id]
        except KeyError as exc:
            raise ValueError(f"Unknown planned task: {task_id!r}") from exc

    def _validate_result_attribution(
        self,
        result: ResearchTaskResult,
    ) -> ResearchTaskSpec:
        if result.mission_id != self.mission_id:
            raise ValueError("result mission_id does not match this mission")
        task = self._planned_task(result.task_id)
        if result.role is not task.role:
            raise ValueError("result role does not match the planned task role")
        return task

    def _required_prerequisite_ids(self, task: ResearchTaskSpec) -> tuple[str, ...]:
        prerequisite_ids = set(task.depends_on)
        if task.role is ResearchRole.CHALLENGER:
            prerequisite_ids.update(
                planned.task_id
                for planned in self._tasks
                if planned.role is ResearchRole.RESEARCHER
            )
        elif task.role is ResearchRole.JUDGE:
            prerequisite_ids.update(
                planned.task_id
                for planned in self._tasks
                if planned.role in (ResearchRole.RESEARCHER, ResearchRole.CHALLENGER)
            )
        prerequisite_ids.discard(task.task_id)
        return tuple(
            planned.task_id
            for planned in self._tasks
            if planned.task_id in prerequisite_ids
        )

    def _prerequisites_satisfied_locked(self, task: ResearchTaskSpec) -> bool:
        return all(
            (
                prerequisite := self._results_by_task_id.get(prerequisite_id)
            ) is not None
            and prerequisite.status is ResearchTaskResultStatus.COMPLETED
            for prerequisite_id in self._required_prerequisite_ids(task)
        )

    @staticmethod
    def _copy_evidence_batch(evidence) -> tuple[ResearchEvidence, ...]:
        if type(evidence) not in (list, tuple):
            raise TypeError("evidence must be a list or tuple of ResearchEvidence values")
        return tuple(_copy_evidence(item) for item in evidence)

    def _validate_evidence_batch_locked(
        self,
        candidates: tuple[ResearchEvidence, ...],
        *,
        expected_task_id: str | None = None,
        allow_unregistered_task_id: str | None = None,
    ) -> None:
        candidate_ids = [item.evidence_id for item in candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ResearchEvidenceConflictError(
                "duplicate evidence_id within submitted evidence batch"
            )
        duplicate_ids = sorted(set(candidate_ids) & set(self._evidence_by_id))
        if duplicate_ids:
            raise ResearchEvidenceConflictError(
                f"duplicate evidence_id already registered: {duplicate_ids[0]!r}"
            )

        for item in candidates:
            if item.mission_id != self.mission_id:
                raise ValueError("evidence mission_id does not match this mission")
            task = self._planned_task(item.task_id)
            if expected_task_id is not None and task.task_id != expected_task_id:
                raise ValueError("evidence task_id does not match the submitted result task")
            if (
                task.task_id not in self._results_by_task_id
                and task.task_id != allow_unregistered_task_id
            ):
                raise ValueError(
                    "evidence requires an existing result for its exact planned task"
                )

    def _results_locked(self) -> tuple[ResearchTaskResult, ...]:
        return tuple(
            self._results_by_task_id[task.task_id]
            for task in self._tasks
            if task.task_id in self._results_by_task_id
        )

    @staticmethod
    def _copy_tasks(
        tasks: tuple[ResearchTaskSpec, ...],
    ) -> tuple[ResearchTaskSpec, ...]:
        return tuple(_copy_task_spec(task) for task in tasks)

    @staticmethod
    def _copy_results(
        results: tuple[ResearchTaskResult, ...],
    ) -> tuple[ResearchTaskResult, ...]:
        return tuple(_copy_result(result) for result in results)

    @staticmethod
    def _copy_evidence_items(
        evidence: tuple[ResearchEvidence, ...],
    ) -> tuple[ResearchEvidence, ...]:
        return tuple(_copy_evidence(item) for item in evidence)

    def _require_mission_contract_locked(self) -> None:
        try:
            current_mission_id = _identifier(
                self._mission.mission_id,
                "mission_id",
            )
        except (TypeError, ValueError) as exc:
            raise ResearchMissionContractError(
                "mission identity no longer matches the captured contract"
            ) from exc
        if current_mission_id != self.mission_id:
            raise ResearchMissionContractError(
                "mission identity no longer matches the captured contract"
            )
        if self._mission.plan is not self._source_plan:
            raise ResearchMissionContractError(
                "mission plan no longer matches the captured contract"
            )
        try:
            current_plan_identity = _plan_contract_identity(self._source_plan)
        except (TypeError, ValueError) as exc:
            raise ResearchMissionContractError(
                "mission plan no longer matches the captured contract"
            ) from exc
        if current_plan_identity != self._plan_identity:
            raise ResearchMissionContractError(
                "mission plan no longer matches the captured contract"
            )
        if type(self._mission.status) is not ResearchMissionStatus:
            raise ResearchMissionContractError(
                "mission status is not a ResearchMissionStatus"
            )

    def _results_with_status_locked(
        self,
        status: ResearchTaskResultStatus,
    ) -> tuple[ResearchTaskResult, ...]:
        return tuple(
            result for result in self._results_locked() if result.status is status
        )

    def _unsatisfied_tasks_locked(self) -> tuple[ResearchTaskSpec, ...]:
        return tuple(
            task
            for task in self._tasks
            if (
                result := self._results_by_task_id.get(task.task_id)
            ) is None
            or result.status is not ResearchTaskResultStatus.COMPLETED
        )

    def _evidence_locked(self) -> tuple[ResearchEvidence, ...]:
        return tuple(
            sorted(
                self._evidence_by_id.values(),
                key=lambda item: (
                    self._sequence_by_task_id[item.task_id],
                    item.evidence_id,
                ),
            )
        )

    def _completion_eligible_locked(self) -> bool:
        return (
            not self._unsatisfied_tasks_locked()
            and (
                self._mission.status is ResearchMissionStatus.ROUTED
                or self._mission.status is ResearchMissionStatus.DISPATCHED
            )
        )
