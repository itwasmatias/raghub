from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import sys
from pathlib import Path, PurePosixPath

from ._locking import FileLock
from .models import Task
from .remote import LocalRunner, SSHRunner


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path | PurePosixPath
    branch: str
    base_commit: str
    dirty: bool = False
    dirty_paths: tuple[str, ...] = field(default_factory=tuple)


class WorktreeManager:
    MARKER = ".raghub-controller-owner.json"

    def __init__(
        self,
        runner: LocalRunner,
        repository: str | Path,
        worktree_root: str | Path,
        *,
        lock_root: str | Path | None = None,
    ) -> None:
        self.runner = runner
        if isinstance(runner, SSHRunner):
            self.repository = PurePosixPath(os.fspath(repository))
            self.worktree_root = PurePosixPath(os.fspath(worktree_root))
        else:
            self.repository = Path(repository)
            self.worktree_root = Path(worktree_root)
        lock_base = os.fspath(lock_root) if lock_root is not None else self.worktree_root
        self.lock_path = Path(lock_base) / ".worktrees.lock"

    def _python(self) -> str:
        return getattr(self.runner, "remote_python", sys.executable)

    def _run_python(
        self,
        script: str,
        *,
        cwd: str | Path,
        input_text: str | None = None,
        timeout_seconds: float = 60,
        args: list[str] | None = None,
    ):
        return self.runner.run(
            [self._python(), "-c", script, *(args or [])],
            cwd=cwd,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )

    def _path_exists(self, path: str | Path) -> bool:
        result = self._run_python(
            "import os,sys; print('1' if os.path.exists(sys.argv[1]) else '0')",
            cwd=self.repository,
            args=[os.fspath(path)],
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or result.stdout or f"failed to stat {path}")
        return result.stdout.strip() == "1"

    def _read_text(self, path: str | Path) -> str:
        result = self._run_python(
            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))",
            cwd=self.repository,
            args=[os.fspath(path)],
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or result.stdout or f"failed to read {path}")
        return result.stdout

    def _write_text(self, path: str | Path, text: str) -> None:
        script = (
            "import pathlib,sys\n"
            "target=pathlib.Path(sys.argv[1])\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            "target.write_text(sys.stdin.read(), encoding='utf-8')\n"
        )
        result = self._run_python(
            script,
            cwd=self.repository,
            args=[os.fspath(path)],
            input_text=text,
            timeout_seconds=60,
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or result.stdout or f"failed to write {path}")

    def _git(self, argv: list[str], *, cwd: str | Path | None = None, timeout_seconds: float = 120) -> str:
        result = self.runner.run(
            ["git", *argv], cwd=cwd or self.repository, timeout_seconds=timeout_seconds
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or result.stdout or f"git failed: {argv!r}")
        return result.stdout.strip()

    def _status(self, path: str | Path) -> tuple[bool, tuple[str, ...]]:
        result = self.runner.run(
            ["git", "status", "--porcelain"], cwd=path, timeout_seconds=60
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr or result.stdout or "git status failed")
        lines = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
        dirty_paths_list: list[str] = []
        for line in lines:
            path_fragment = line[3:].strip() if len(line) > 3 else line
            if path_fragment in {self.MARKER, f"./{self.MARKER}"}:
                continue
            if path_fragment.endswith(f"/{self.MARKER}"):
                continue
            dirty_paths_list.append(path_fragment)
        return bool(dirty_paths_list), tuple(dirty_paths_list)

    def _marker_payload(self, task: Task, branch: str, base_commit: str) -> dict[str, str]:
        return {"task_id": task.id, "branch": branch, "base_commit": base_commit}

    def create(self, task: Task) -> WorktreeInfo:
        branch = f"controller/{task.id}"
        path = self.worktree_root / task.id
        marker = path / self.MARKER
        with FileLock(self.lock_path):
            base_commit = self._git(["rev-parse", f"{task.base_ref}^{{commit}}"])
            expected = self._marker_payload(task, branch, base_commit)
            if self._path_exists(path):
                if not self._path_exists(marker):
                    raise RuntimeError(f"refusing unrelated existing directory: {path}")
                data = json.loads(self._read_text(marker))
                if data != expected:
                    raise RuntimeError(f"worktree ownership mismatch: {path}")
                dirty, dirty_paths = self._status(path)
                return WorktreeInfo(path, branch, base_commit, dirty=dirty, dirty_paths=dirty_paths)
            branch_check = self.runner.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=self.repository,
                timeout_seconds=30,
            )
            if branch_check.exit_code == 0:
                raise RuntimeError(f"branch collision for unrelated task: {branch}")
            self.runner.run(
                [
                    self._python(),
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).mkdir(parents=True, exist_ok=True)",
                    os.fspath(self.worktree_root),
                ],
                cwd=self.repository,
                timeout_seconds=60,
            )
            self._git(["worktree", "add", "-b", branch, os.fspath(path), base_commit])
            self._write_text(marker, json.dumps(expected, indent=2, ensure_ascii=False) + "\n")
            return WorktreeInfo(path, branch, base_commit)

    def reset(self, info: WorktreeInfo) -> None:
        self._git(["reset", "--hard", info.base_commit], cwd=info.path)
        self._git(["clean", "-fdx"], cwd=info.path)

    def cleanup(self, info: WorktreeInfo, *, task_id: str) -> None:
        marker = info.path / self.MARKER
        if not self._path_exists(marker):
            raise RuntimeError("refusing to remove worktree without ownership marker")
        data = json.loads(self._read_text(marker))
        if data.get("task_id") != task_id:
            raise RuntimeError("refusing to remove worktree owned by another task")
        self._git(["worktree", "remove", os.fspath(info.path)], cwd=self.repository)
