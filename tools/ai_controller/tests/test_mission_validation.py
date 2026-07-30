from __future__ import annotations

from tools.ai_controller.mission import (
    ApprovalPolicy,
    FailurePolicy,
    MilestoneDefinition,
    MissionBudget,
    MissionDefinition,
    MissionTaskDefinition,
    ProviderPolicy,
    validate_mission,
)


def build_mission_definition() -> MissionDefinition:
    return MissionDefinition(
        mission_id="mission-alpha",
        title="Mission Alpha",
        description="Validate round-tripping and schema checks.",
        base_ref="HEAD",
        repository_path="/tmp/repo",
        tasks=[
            MissionTaskDefinition(
                task_id="task-a",
                title="Task A",
                prompt="Add a note with backticks `code` and caf\u00e9.",
                depends_on=[],
                tests=[["python", "-c", "print('ok')"]],
                provider_preference="codex",
                provider_fallback=["local-ollama"],
                provider_policy=ProviderPolicy.any_order,
                failure_policy=FailurePolicy.block_dependents,
                approval_policy=ApprovalPolicy.no_approval_required,
                metadata={"owner": "controller"},
            ),
            MissionTaskDefinition(
                task_id="task-b",
                title="Task B",
                prompt="Follow up after task-a.",
                depends_on=["task-a"],
                tests=[["python", "-c", "print('still ok')"]],
                max_attempts=2,
            ),
        ],
        milestones=[
            MilestoneDefinition(
                milestone_id="phase-1",
                title="Phase 1",
                task_ids=["task-a", "task-b"],
            )
        ],
        budgets=MissionBudget(
            max_active_tasks=2,
            max_queued_tasks=4,
            max_total_attempts=8,
            max_failed_tasks=2,
            max_runtime_seconds=3600.0,
            max_worktrees=1,
        ),
        metadata={"source": "unit-test"},
        created_at="2026-07-30T12:00:00Z",
    )


def test_mission_definition_round_trip_preserves_schema_fields():
    definition = build_mission_definition()

    round_tripped = MissionDefinition.from_dict(definition.to_dict())

    assert round_tripped.to_dict() == definition.to_dict()
    assert validate_mission(round_tripped).valid


def test_validate_mission_rejects_missing_task_id():
    definition = build_mission_definition()
    definition.tasks[0] = MissionTaskDefinition(
        task_id="",
        title="Task A",
        prompt="Add a note.",
        tests=[["python", "-c", "print('ok')"]],
    )

    result = validate_mission(definition)

    assert not result.valid
    assert any(error.code == "TASK_ID_EMPTY" for error in result.errors)


def test_validate_mission_rejects_duplicate_task_ids():
    definition = build_mission_definition()
    definition.tasks[1] = MissionTaskDefinition(
        task_id="task-a",
        title="Task B",
        prompt="Duplicate the task id.",
        depends_on=["task-a"],
        tests=[["python", "-c", "print('still ok')"]],
    )

    result = validate_mission(definition)

    assert not result.valid
    assert any(error.code == "TASK_ID_DUPLICATE" for error in result.errors)


def test_validate_mission_rejects_string_tests_from_json_payload():
    payload = build_mission_definition().to_dict()
    payload["tasks"][0]["tests"] = "python -m pytest"

    definition = MissionDefinition.from_dict(payload)
    result = validate_mission(definition)

    assert not result.valid
    assert any(error.code == "TASK_TESTS_NOT_LIST" for error in result.errors)
