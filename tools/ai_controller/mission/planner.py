"""
Deterministic planning helper.

Converts a structured milestone outline dict (authored by humans or AI) into
a validated MissionDefinition.  No I/O beyond the two explicit file helpers.
"""

from __future__ import annotations

import json
from typing import Any

from .models import (
    SCHEMA_VERSION,
    MissionBudget,
    MissionDefinition,
    MilestoneDefinition,
    MissionTaskDefinition,
    ValidationResult,
)
from .validation import validate_mission


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_tasks_from_phases(
    phases: list[dict[str, Any]],
    base_ref: str,
) -> tuple[list[MissionTaskDefinition], list[MilestoneDefinition]]:
    """
    Convert phase/task outline entries into flat MissionTaskDefinition and
    MilestoneDefinition lists.

    Rules
    -----
    - Serial phase (parallel=False): each task depends on every task in the
      *previous* phase, creating a happens-before barrier at phase boundaries.
    - Parallel phase (parallel=True): tasks share the same prev-phase deps
      but have no intra-phase dependencies with each other.
    - A task-level "depends_on" key overrides the auto-computed deps when
      present and non-empty.
    """
    tasks: list[MissionTaskDefinition] = []
    milestones: list[MilestoneDefinition] = []

    # task_ids produced by the previous phase — used to wire inter-phase deps.
    prev_phase_task_ids: list[str] = []

    for phase in phases:
        phase_id: str = phase["phase_id"]
        phase_title: str = phase.get("title", phase_id)
        parallel: bool = bool(phase.get("parallel", False))
        phase_task_specs: list[dict[str, Any]] = phase.get("tasks", [])

        # Collect all task_ids for this phase (for milestone and next-phase deps).
        phase_task_ids: list[str] = [spec["task_id"] for spec in phase_task_specs]

        for idx, spec in enumerate(phase_task_specs):
            task_id: str = spec["task_id"]

            # ---- dependency resolution ------------------------------------
            explicit_deps: list[str] = spec.get("depends_on", [])
            if explicit_deps:
                # Caller provided explicit overrides — use them verbatim.
                computed_deps: list[str] = list(explicit_deps)
            elif parallel:
                # Parallel phase: each task only inherits prev-phase barrier,
                # no dependency on sibling tasks within the same phase.
                computed_deps = list(prev_phase_task_ids)
            else:
                # Serial phase: first task inherits prev-phase barrier; each
                # subsequent task also depends on the immediately preceding task
                # within this phase.
                computed_deps = list(prev_phase_task_ids)
                if idx > 0:
                    computed_deps.append(phase_task_ids[idx - 1])

            # Deduplicate while preserving order.
            seen: set[str] = set()
            deduped_deps: list[str] = []
            for dep in computed_deps:
                if dep not in seen:
                    seen.add(dep)
                    deduped_deps.append(dep)

            # ---- build task definition ------------------------------------
            task = MissionTaskDefinition(
                task_id=task_id,
                title=spec.get("title", task_id),
                prompt=spec.get("prompt", ""),
                depends_on=deduped_deps,
                base_ref=spec.get("base_ref", base_ref),
                tests=[list(t) for t in spec.get("tests", [])],
                provider_preference=spec.get("provider_preference"),
                max_attempts=int(spec.get("max_attempts", 3)),
                failure_policy=spec.get("failure_policy", "block_dependents"),  # type: ignore[arg-type]
                metadata=dict(spec.get("metadata", {})),
            )
            tasks.append(task)

        # ---- build milestone for this phase --------------------------------
        milestone = MilestoneDefinition(
            milestone_id=phase_id,
            title=phase_title,
            description=phase.get("description", ""),
            task_ids=phase_task_ids,
        )
        milestones.append(milestone)

        # The current phase becomes the barrier for the next phase.
        prev_phase_task_ids = list(phase_task_ids)

    return tasks, milestones


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def outline_to_mission(outline: dict[str, Any]) -> tuple[MissionDefinition, ValidationResult]:
    """
    Convert a structured outline dict to a MissionDefinition.

    Outline format
    --------------
    {
        "mission_id": str,
        "title": str,
        "description": str,
        "base_ref": str,
        "repository_path": str,
        "budgets": {...},    # optional — defaults applied by MissionBudget
        "metadata": {...},   # optional
        "phases": [
            {
                "phase_id": str,       # becomes milestone_id
                "title": str,
                "parallel": bool,      # if True, tasks run in parallel
                "tasks": [
                    {
                        "task_id": str,
                        "title": str,
                        "prompt": str,
                        "tests": [...],
                        "max_attempts": int,
                        "failure_policy": str,
                        "depends_on": [...],   # explicit override (optional)
                        "metadata": {...},
                    }
                ]
            }
        ]
    }

    Dependency rules
    ----------------
    - Serial phase: each task depends on all tasks in the previous phase plus
      the immediately preceding sibling within the same phase.
    - Parallel phase: tasks depend only on the previous phase's tasks.
    - A non-empty "depends_on" in a task spec overrides auto-computation.

    Returns
    -------
    (MissionDefinition, ValidationResult)
        The definition is always returned even when validation fails so that
        the caller can inspect or repair the errors.
    """
    mission_id: str = outline.get("mission_id", "")
    title: str = outline.get("title", "")
    description: str = outline.get("description", "")
    base_ref: str = outline.get("base_ref", "HEAD")
    repository_path: str = outline.get("repository_path", "")
    metadata: dict[str, Any] = dict(outline.get("metadata", {}))

    budgets_data = outline.get("budgets")
    budgets = MissionBudget.from_dict(budgets_data) if budgets_data else MissionBudget()

    phases: list[dict[str, Any]] = outline.get("phases", [])
    tasks, milestones = _build_tasks_from_phases(phases, base_ref)

    definition = MissionDefinition(
        mission_id=mission_id,
        title=title,
        description=description,
        schema_version=SCHEMA_VERSION,
        base_ref=base_ref,
        repository_path=repository_path,
        tasks=tasks,
        milestones=milestones,
        budgets=budgets,
        metadata=metadata,
    )

    result = validate_mission(definition)
    return definition, result


def load_outline_file(path: str) -> dict[str, Any]:
    """Load a JSON outline file from *path* and return the parsed dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_mission_file(definition: MissionDefinition, path: str) -> None:
    """Serialise *definition* to JSON and write it to *path*."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(definition.to_dict(), fh, indent=2, ensure_ascii=False)
        fh.write("\n")
