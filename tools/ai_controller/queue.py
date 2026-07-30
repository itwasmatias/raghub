from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ._locking import FileLock
from .models import Task


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class DurableQueue:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        for name in ("pending", "running", "succeeded", "failed", "invalid"):
            setattr(self, name, self.root / name)
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".queue.lock"

    def enqueue(self, task: Task) -> Path:
        with FileLock(self.lock_path):
            if any((directory / f"{task.id}.json").exists() for directory in (
                self.pending, self.running, self.succeeded, self.failed
            )):
                raise FileExistsError(f"task already exists: {task.id}")
            destination = self.pending / f"{task.id}.json"
            atomic_json(destination, task.to_dict())
            return destination

    def claim_next(self) -> Task | None:
        with FileLock(self.lock_path):
            for source in sorted(self.pending.glob("*.json")):
                try:
                    task = Task.from_dict(json.loads(source.read_text(encoding="utf-8")))
                except (ValueError, TypeError, json.JSONDecodeError, OSError):
                    os.replace(source, self.invalid / source.name)
                    continue
                destination = self.running / source.name
                try:
                    os.replace(source, destination)
                except FileNotFoundError:
                    continue
                return task
        return None

    def finish(self, task: Task, *, succeeded: bool) -> None:
        with FileLock(self.lock_path):
            source = self.running / f"{task.id}.json"
            destination = (self.succeeded if succeeded else self.failed) / source.name
            os.replace(source, destination)

    def recover_orphans(self, *, older_than_seconds: float) -> list[str]:
        recovered: list[str] = []
        cutoff = time.time() - older_than_seconds
        with FileLock(self.lock_path):
            for source in sorted(self.running.glob("*.json")):
                if source.stat().st_mtime > cutoff:
                    continue
                destination = self.pending / source.name
                os.replace(source, destination)
                recovered.append(source.stem)
        return recovered
