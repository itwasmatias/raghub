from __future__ import annotations

from pathlib import Path

from ._locking import FileLock
from .queue import atomic_json


def report_lock_path(root: Path) -> Path:
    return Path(root) / ".reports.lock"


class ReportStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, task_id: str, payload: dict) -> Path:
        path = self.root / f"{task_id}.json"
        with FileLock(report_lock_path(self.root)):
            atomic_json(path, payload)
        return path
