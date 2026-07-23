from dataclasses import dataclass


@dataclass(slots=True)
class PlayerSeasonStats:
    player_id: str
    player_name: str
    season: str
    games_played: int
    points: float
    rebounds: float
    assists: float
    minutes: float
    field_goals_made: float
    field_goals_attempted: float
    three_points_made: float
    three_points_attempted: float
    free_throws_made: float
    free_throws_attempted: float

    @property
    def points_per_game(self) -> float:
        return self._per_game(self.points)

    @property
    def rebounds_per_game(self) -> float:
        return self._per_game(self.rebounds)

    @property
    def assists_per_game(self) -> float:
        return self._per_game(self.assists)

    @property
    def minutes_per_game(self) -> float:
        return self._per_game(self.minutes)

    @property
    def field_goal_percentage(self) -> float:
        return self._percentage(
            self.field_goals_made,
            self.field_goals_attempted,
        )

    @property
    def three_point_percentage(self) -> float:
        return self._percentage(
            self.three_points_made,
            self.three_points_attempted,
        )

    @property
    def free_throw_percentage(self) -> float:
        return self._percentage(
            self.free_throws_made,
            self.free_throws_attempted,
        )

    def _per_game(self, total: float) -> float:
        if self.games_played == 0:
            return 0.0
        return total / self.games_played

    @staticmethod
    def _percentage(made: float, attempted: float) -> float:
        if attempted == 0:
            return 0.0
        return made / attempted
