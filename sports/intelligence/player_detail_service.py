from typing import Any

from sports.data.repositories.sqlite_player_game_log_repository import (
    SQLitePlayerGameLogRepository,
)
from sports.intelligence.models.player_intelligence import PlayerIntelligence
from sports.intelligence.player_intelligence_service import (
    PlayerIntelligenceService,
)


class PlayerDetailService:
    def __init__(
        self,
        game_log_repository: SQLitePlayerGameLogRepository,
        intelligence_service: PlayerIntelligenceService | None = None,
    ) -> None:
        self.game_log_repository = game_log_repository
        self.intelligence_service = (
            intelligence_service or PlayerIntelligenceService()
        )

    def get(
        self,
        player_id: str,
        seasons: list[str],
    ) -> dict[str, Any] | None:
        logs = [
            row
            for season in seasons
            for row in self.game_log_repository.list_by_player(player_id, season)
        ]
        intelligence = self.intelligence_service.analyze(
            player_id,
            logs,
            seasons,
        )
        if intelligence is None:
            return None
        return {
            "player_id": player_id,
            "player_name": intelligence.player_name,
            "games": sorted(
                logs,
                key=lambda row: str(row.get("game_date") or ""),
            ),
            "trend_chart": [
                {
                    "date": row.get("game_date"),
                    "points": row.get("pts"),
                    "minutes": row.get("minutes"),
                    "attempts": row.get("fga"),
                }
                for row in sorted(
                    logs,
                    key=lambda row: str(row.get("game_date") or ""),
                )
            ],
            "intelligence": intelligence,
            "explanation": intelligence.explanation,
            "evidence": intelligence.evidence,
        }
