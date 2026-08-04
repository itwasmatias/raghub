from __future__ import annotations

import json
from pathlib import Path

from tools.ai_controller.cli import _mission_command, parser
from tools.ai_controller.config import ControllerConfig
from tools.ai_controller.mission.models import (
    ApprovalPolicy,
    MissionDefinition,
    MissionState,
    MissionStatus,
    MissionTaskDefinition,
    MissionTaskState,
    MissionTaskStatus,
)
from tools.ai_controller.mission.repair import plan_mission_repairs
from tools.ai_controller.mission.store import MissionStore
from tools.ai_controller.queue import DurableQueue


def _settings(tmp_path: Path) -> ControllerConfig:
    return ControllerConfig(
        controller_root=tmp_path / "controller",
        repository_path=tmp_path / "repository",
        worktree_root=tmp_path / "worktrees",
        codex_attempts=0,
        local_attempts=0,
    )


def _mission(
    settings: ControllerConfig,
    *,
    approval_policy: ApprovalPolicy = ApprovalPolicy.no_approval_required,
) -> None:
    definition = MissionDefinition(
        mission_id="cli-repair",
        title="CLI repair",
        tasks=[
            MissionTaskDefinition(
                task_id="task-a",
                title="Task A",
                prompt="Offline fixture task",
                approval_policy=approval_policy,
            )
        ],
        created_at="2026-08-04T12:00:00+00:00",
    )
    state = MissionState(
        mission_id=definition.mission_id,
        status=MissionStatus.running,
        task_states={
            "task-a": MissionTaskState(
                task_id="task-a",
                status=(
                    MissionTaskStatus.approval_required
                    if approval_policy
                    != ApprovalPolicy.no_approval_required
                    else MissionTaskStatus.pending
                ),
            )
        },
    )
    MissionStore(settings.missions_root).create(definition, state)


def _args(tmp_path: Path, *repair_args: str):
    return parser().parse_args(
        [
            "--config",
            str(tmp_path / "config.json"),
            "mission",
            "repair",
            *repair_args,
        ]
    )


def test_repair_cli_defaults_to_read_only_list(
    tmp_path: Path,
    capsys,
):
    settings = _settings(tmp_path)
    _mission(settings)

    result = _mission_command(_args(tmp_path, "cli-repair"), settings)
    output = capsys.readouterr().out

    assert result == 0
    assert "DRY-RUN" in output
    assert "repair-" in output
    assert "cli-repair" in output
    assert "task-a" in output
    assert "SAFE_REPAIR" in output
    assert "REMATERIALIZE_MISSING_QUEUE_TASK" in output
    assert "eligible task" in output
    assert not list(settings.queue_root.rglob("*.json"))


def test_repair_cli_dry_run_with_absent_roots_creates_nothing(
    tmp_path: Path,
    capsys,
):
    settings = _settings(tmp_path)
    tracked_paths = [
        settings.queue_root,
        settings.missions_root,
        settings.reports_root,
        settings.repository_path,
    ]

    result = _mission_command(
        _args(tmp_path, "missing-mission", "--json"),
        settings,
    )
    error = capsys.readouterr().err

    assert result == 1
    assert "not found" in error
    assert all(not Path(path).exists() for path in tracked_paths)
    assert not list(tmp_path.rglob("*.lock"))
    assert not list(tmp_path.rglob("*.jsonl"))
    assert not settings.controller_root.exists()


def test_repair_cli_requires_explicit_single_action_apply(
    tmp_path: Path,
    capsys,
):
    settings = _settings(tmp_path)
    _mission(settings)
    store = MissionStore(settings.missions_root)
    queue = DurableQueue(settings.queue_root)
    action = plan_mission_repairs(
        "cli-repair", store, queue, settings.reports_root
    )[0]

    result = _mission_command(
        _args(tmp_path, "cli-repair", "--apply", action.action_id),
        settings,
    )
    output = capsys.readouterr().out

    assert result == 0
    assert "APPLIED" in output
    assert action.action_id in output
    assert len(list(queue.pending.glob("*.json"))) == 1


def test_repair_cli_json_is_deterministic(
    tmp_path: Path,
    capsys,
):
    settings = _settings(tmp_path)
    _mission(settings)
    args = _args(tmp_path, "cli-repair", "--json")

    assert _mission_command(args, settings) == 0
    first = capsys.readouterr().out
    assert _mission_command(args, settings) == 0
    second = capsys.readouterr().out

    assert first == second
    payload = json.loads(first)
    assert payload["mode"] == "dry-run"
    assert payload["mission_id"] == "cli-repair"
    assert payload["actions"][0]["classification"] == "SAFE_REPAIR"
    assert payload["actions"][0]["proposed_action"] == (
        "REMATERIALIZE_MISSING_QUEUE_TASK"
    )


def test_repair_cli_displays_but_refuses_unsafe_action(
    tmp_path: Path,
    capsys,
):
    settings = _settings(tmp_path)
    _mission(
        settings,
        approval_policy=ApprovalPolicy.approval_required_before_queue,
    )
    store = MissionStore(settings.missions_root)
    queue = DurableQueue(settings.queue_root)
    action = plan_mission_repairs(
        "cli-repair", store, queue, settings.reports_root
    )[0]

    dry_run = _mission_command(
        _args(tmp_path, "cli-repair", "--json"),
        settings,
    )
    payload = json.loads(capsys.readouterr().out)
    apply_result = _mission_command(
        _args(tmp_path, "cli-repair", "--apply", action.action_id),
        settings,
    )
    error = capsys.readouterr().err

    assert dry_run == 0
    assert payload["actions"][0]["classification"] == "HUMAN_REVIEW_REQUIRED"
    assert apply_result == 1
    assert "not safe to apply" in error
    assert not list(queue.pending.glob("*.json"))
