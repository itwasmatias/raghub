from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .remote import LocalRunner


@dataclass(frozen=True, order=True)
class GitChange:
    kind: str
    path: str
    old_path: str | None = None


def _run(runner: LocalRunner, repo: Path, argv: list[str]) -> str:
    result = runner.run(argv, cwd=repo, timeout_seconds=60)
    if result.exit_code != 0:
        raise RuntimeError(result.stderr or f"command failed: {argv!r}")
    return result.stdout


def capture_changes(runner: LocalRunner, repo: Path, base_ref: str) -> list[GitChange]:
    raw = _run(
        runner,
        repo,
        ["git", "diff", "--name-status", "-z", "--find-renames", base_ref, "--"],
    )
    parts = raw.split("\0")
    changes: list[GitChange] = []
    index = 0
    kinds = {"M": "modified", "A": "added", "D": "deleted"}
    while index < len(parts) and parts[index]:
        status = parts[index]
        index += 1
        if status.startswith("R"):
            old_path, new_path = parts[index], parts[index + 1]
            index += 2
            changes.append(GitChange("renamed", new_path, old_path))
        else:
            path = parts[index]
            index += 1
            changes.append(GitChange(kinds.get(status[0], "modified"), path))
    untracked = _run(
        runner, repo, ["git", "ls-files", "--others", "--exclude-standard", "-z"]
    )
    changes.extend(
        GitChange("untracked", path)
        for path in untracked.split("\0")
        if path and not path.startswith(".raghub-controller-owner.json")
    )
    return sorted(changes)


def detect_changes(
    runner: LocalRunner,
    repo: Path,
    base_ref: str,
    initial: list[GitChange],
) -> list[GitChange]:
    before = set(initial)
    return [change for change in capture_changes(runner, repo, base_ref) if change not in before]
