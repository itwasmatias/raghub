from collections import defaultdict
from collections.abc import Iterable, Mapping
from math import sqrt
from typing import Any

from sports.intelligence.models.player_intelligence import (
    CompetitionProfile,
    PlayerIntelligence,
    RoleChangeProfile,
    VolatilityProfile,
    WeightedAverages,
)


class PlayerIntelligenceService:
    def analyze(
        self,
        player_id: str,
        game_logs: Iterable[Mapping[str, Any]],
        seasons: list[str],
    ) -> PlayerIntelligence | None:
        rows = [
            row
            for row in game_logs
            if str(row.get("player_id")) == str(player_id)
            and str(row.get("season")) in seasons
        ]
        if not rows:
            return None

        by_competition: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            key = f"{row.get('league', 'NBA')}:{row.get('competition', 'regular')}"
            by_competition[key].append(row)

        profiles = {
            key: self._profile(key, values, seasons)
            for key, values in by_competition.items()
        }
        regular = profiles.get("NBA:regular")
        playoffs = profiles.get("NBA:playoffs")
        playoff_delta = (
            playoffs.baseline.points - regular.baseline.points
            if regular is not None and playoffs is not None
            else None
        )
        primary = regular or next(iter(profiles.values()))
        evidence = [
            f"Recent 5: {primary.recent_five.points:.1f} PPG",
            f"Three-season baseline: {primary.baseline.points:.1f} PPG",
            f"Consistency: {primary.volatility.consistency_score:.0f}/100",
            *primary.role_change.signals,
        ]
        return PlayerIntelligence(
            player_id=str(player_id),
            player_name=str(rows[-1].get("player_name") or ""),
            profiles=profiles,
            playoff_vs_regular_ppg=playoff_delta,
            explanation=(
                f"Recent scoring is "
                f"{primary.recent_five.points - primary.baseline.points:+.1f} "
                "PPG versus the competition-specific long-term baseline."
            ),
            evidence=evidence,
            latest_team=str(
                rows[-1].get("team_name")
                or rows[-1].get("team_abbreviation")
                or ""
            ),
        )

    def _profile(
        self,
        key: str,
        rows: list[Mapping[str, Any]],
        seasons: list[str],
    ) -> CompetitionProfile:
        ordered = sorted(rows, key=lambda row: str(row.get("game_date") or ""))
        current_rows = [
            row for row in ordered if str(row.get("season")) == seasons[-1]
        ]
        previous_rows = (
            [
                row
                for row in ordered
                if str(row.get("season")) == seasons[-2]
            ]
            if len(seasons) > 1
            else []
        )
        league, competition = key.split(":", maxsplit=1)
        return CompetitionProfile(
            league=league,
            competition=competition,
            baseline=self._averages(ordered),
            current=self._averages(current_rows),
            previous=self._averages(previous_rows) if previous_rows else None,
            recent_five=self._averages(ordered[-5:]),
            recent_ten=self._averages(ordered[-10:]),
            volatility=self._volatility(ordered),
            role_change=self._role_change(ordered),
        )

    @classmethod
    def _averages(
        cls,
        rows: list[Mapping[str, Any]],
    ) -> WeightedAverages:
        games = len(rows)
        totals = {
            key: sum(cls._number(row.get(key)) for row in rows)
            for key in (
                "pts", "reb", "ast", "minutes", "fgm", "fga",
                "fg3m", "fg3a", "ftm", "fta",
            )
        }
        denominator = games or 1
        return WeightedAverages(
            games=games,
            points=totals["pts"] / denominator,
            rebounds=totals["reb"] / denominator,
            assists=totals["ast"] / denominator,
            minutes=totals["minutes"] / denominator,
            field_goal_percentage=cls._rate(totals["fgm"], totals["fga"]),
            three_point_percentage=cls._rate(totals["fg3m"], totals["fg3a"]),
            free_throw_percentage=cls._rate(totals["ftm"], totals["fta"]),
        )

    @classmethod
    def _volatility(
        cls,
        rows: list[Mapping[str, Any]],
    ) -> VolatilityProfile:
        values = [cls._number(row.get("pts")) for row in rows]
        mean = sum(values) / len(values)
        deviation = sqrt(
            sum((value - mean) ** 2 for value in values) / len(values)
        )
        coefficient = deviation / mean if mean else 0.0
        strong = sum(value > mean + deviation for value in values)
        weak = sum(value < mean - deviation for value in values)
        return VolatilityProfile(
            standard_deviation=deviation,
            coefficient_of_variation=coefficient,
            consistency_score=max(0.0, min(100.0, 100 * (1 - coefficient))),
            hot_streak_games=cls._tail_streak(values, mean, above=True),
            cold_streak_games=cls._tail_streak(values, mean, above=False),
            unusually_strong_games=strong,
            unusually_weak_games=weak,
        )

    @classmethod
    def _role_change(
        cls,
        rows: list[Mapping[str, Any]],
    ) -> RoleChangeProfile:
        split = max(1, len(rows) // 2)
        earlier = rows[:split]
        recent = rows[split:] or rows[-1:]
        minutes_change = (
            cls._mean(recent, "minutes") - cls._mean(earlier, "minutes")
        )
        usage_change = (
            cls._mean(recent, "fga") - cls._mean(earlier, "fga")
        )
        starters_before = sum(bool(row.get("starter")) for row in earlier)
        starters_after = sum(bool(row.get("starter")) for row in recent)
        starter_change = None
        if starters_after and not starters_before:
            starter_change = "bench_to_starter"
        elif starters_before and not starters_after:
            starter_change = "starter_to_bench"
        teams = [str(row.get("team_id") or "") for row in rows]
        team_changed = len(set(teams)) > 1
        injury_opportunity = any(
            bool(row.get("injury_opportunity")) for row in recent
        )
        short_success = any(
            cls._number(row.get("minutes")) < 20
            and cls._number(row.get("pts")) >= 10
            for row in recent
        )
        signals: list[str] = []
        if minutes_change >= 5:
            signals.append("Minutes increased by at least five per game.")
        if usage_change >= 3:
            signals.append("Shot volume increased.")
        if starter_change:
            signals.append("Starting role changed.")
        if team_changed:
            signals.append("Performance window crosses a team change.")
        if injury_opportunity:
            signals.append("Opportunity followed an injury.")
        if short_success:
            signals.append("Produced efficiently in a short appearance.")
        return RoleChangeProfile(
            minutes_change=minutes_change,
            usage_change=usage_change,
            starter_change=starter_change,
            team_changed=team_changed,
            injury_opportunity=injury_opportunity,
            short_appearance_success=short_success,
            signals=signals,
        )

    @classmethod
    def _mean(cls, rows: list[Mapping[str, Any]], key: str) -> float:
        return sum(cls._number(row.get(key)) for row in rows) / len(rows)

    @staticmethod
    def _tail_streak(
        values: list[float],
        baseline: float,
        *,
        above: bool,
    ) -> int:
        count = 0
        for value in reversed(values):
            matches = value > baseline if above else value < baseline
            if not matches:
                break
            count += 1
        return count

    @staticmethod
    def _rate(made: float, attempted: float) -> float:
        return made / attempted if attempted else 0.0

    @staticmethod
    def _number(value: Any) -> float:
        if value in (None, ""):
            return 0.0
        if isinstance(value, str) and ":" in value:
            minutes, seconds = value.split(":", maxsplit=1)
            return float(minutes) + float(seconds) / 60
        return float(value)
