from __future__ import annotations

import json
from pathlib import Path

from ._locking import FileLock


class ExperienceLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe = {
            key: value
            for key, value in payload.items()
            if key not in {"prompt", "original_task_prompt"}
        }
        with FileLock(self.path.with_suffix(self.path.suffix + ".lock")):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(safe, ensure_ascii=False) + "\n")
