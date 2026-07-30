from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import ProviderResult, Task


class Provider(ABC):
    name: str

    @abstractmethod
    def execute(self, *, task: Task, worktree: Path, attempt: int) -> ProviderResult:
        raise NotImplementedError
