from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RefreshSummary:
    season: str
    games_processed: int
    players_saved: int
