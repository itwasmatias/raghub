from __future__ import annotations

from pathlib import Path

from .queue import atomic_json


class ReportStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, task_id: str, payload: dict) -> Path:
        path = self.root / f"{task_id}.json"
        atomic_json(path, payload)
        return path
