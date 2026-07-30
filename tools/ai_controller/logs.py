from __future__ import annotations

import json
from pathlib import Path

from ._locking import FileLock


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with FileLock(lock_path):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
