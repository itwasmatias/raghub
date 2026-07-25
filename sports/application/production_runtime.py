from __future__ import annotations

from typing import Any, Protocol

from sports.application.production_basketball_runtime import (
    ProductionBasketballRuntime,
)


class ApplicationRuntime(Protocol):
    def get_betting_board(self) -> dict[str, Any]: ...
    def get_betting_replay_board(self) -> dict[str, Any]: ...
    def get_dashboard_snapshot(self, **kwargs: Any) -> dict[str, Any]: ...
    def refresh(self) -> None: ...


def build_production_runtime(**dependencies: Any) -> ApplicationRuntime:
    return ProductionBasketballRuntime(**dependencies)
