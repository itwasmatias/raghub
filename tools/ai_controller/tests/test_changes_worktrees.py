from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.ai_controller.changes import capture_changes, detect_changes
from tools.ai_controller.models import Task
from tools.ai_controller.remote import LocalRunner
from tools.ai_controller.worktrees import WorktreeManager


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "controller@example.test")
    git(repo, "config", "user.name", "Controller Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / "modify.txt").write_text("before\n", encoding="utf-8")
    (repo / "delete.txt").write_text("delete\n", encoding="utf-8")
    (repo / "rename.txt").write_text("rename\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def test_git_change_detection_finds_modified_added_deleted_renamed_untracked(tmp_path):
    repo, base = make_repo(tmp_path)
    initial = capture_changes(LocalRunner(), repo, base)
    (repo / "modify.txt").write_text("after\n", encoding="utf-8")
    (repo / "added.txt").write_text("added\n", encoding="utf-8")
    git(repo, "add", "added.txt")
    (repo / "delete.txt").unlink()
    git(repo, "mv", "rename.txt", "renamed file.txt")
    (repo / "templates").mkdir()
    (repo / "templates" / "sip_markets.html").write_text("exact\n", encoding="utf-8")

    changes = detect_changes(LocalRunner(), repo, base, initial)
    by_kind = {(item.kind, item.path, item.old_path) for item in changes}

    assert ("modified", "modify.txt", None) in by_kind
    assert ("added", "added.txt", None) in by_kind
    assert ("deleted", "delete.txt", None) in by_kind
    assert ("renamed", "renamed file.txt", "rename.txt") in by_kind
    assert ("untracked", "templates/sip_markets.html", None) in by_kind


def test_existing_unrelated_dirty_file_is_not_attributed(tmp_path):
    repo, base = make_repo(tmp_path)
    (repo / "keep.txt").write_text("user dirty\n", encoding="utf-8")
    initial = capture_changes(LocalRunner(), repo, base)
    (repo / "modify.txt").write_text("task\n", encoding="utf-8")

    changes = detect_changes(LocalRunner(), repo, base, initial)

    assert [item.path for item in changes] == ["modify.txt"]


def test_worktree_creation_is_isolated_idempotent_and_collision_safe(tmp_path):
    repo, base = make_repo(tmp_path)
    manager = WorktreeManager(
        runner=LocalRunner(),
        repository=repo,
        worktree_root=tmp_path / "worktrees",
    )
    item = task = Task(
        id="task-path-safe",
        title="Task",
        prompt="Prompt",
        base_ref=base,
        tests=[],
    )

    first = manager.create(item)
    second = manager.create(item)

    assert first == second
    assert first.path != repo
    assert git(first.path, "rev-parse", "--show-toplevel") == str(first.path)
    assert first.branch == "controller/task-path-safe"
    assert (first.path / ".raghub-controller-owner.json").exists()


def test_worktree_manager_refuses_unrelated_existing_directory(tmp_path):
    repo, base = make_repo(tmp_path)
    root = tmp_path / "worktrees"
    collision = root / "task-collision"
    collision.mkdir(parents=True)
    manager = WorktreeManager(LocalRunner(), repo, root)

    with pytest.raises(RuntimeError, match="unrelated"):
        manager.create(
            Task(
                id="task-collision",
                title="Task",
                prompt="Prompt",
                base_ref=base,
                tests=[],
            )
        )
