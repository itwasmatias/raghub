from collections import defaultdict
from collections.abc import Iterable, Mapping
from math import sqrt, tanh
from typing import Any

from sports.intelligence.models.player_intelligence import (
    AdvancedPlayerFeatures,
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
            qualitative_evidence=self._qualitative_evidence(rows),
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
            recent_three=self._averages(ordered[-3:]),
            recent_five=self._averages(ordered[-5:]),
            recent_ten=self._averages(ordered[-10:]),
            splits=self._splits(ordered),
            advanced=self._advanced(ordered),
            volatility=self._volatility(ordered),
            role_change=self._role_change(ordered),
        )

    @classmethod
    def _splits(
        cls, rows: list[Mapping[str, Any]]
    ) -> dict[str, WeightedAverages]:
        groups: dict[str, list[Mapping[str, Any]]] = {
            "home": [row for row in rows if bool(row.get("home"))],
            "away": [row for row in rows if not bool(row.get("home"))],
            "starter": [row for row in rows if bool(row.get("starter"))],
            "bench": [row for row in rows if not bool(row.get("starter"))],
            "wins": [row for row in rows if bool(row.get("win"))],
            "losses": [row for row in rows if not bool(row.get("win"))],
            "rest_0_days": [
                row for row in rows if cls._number(row.get("rest_days")) == 0
            ],
            "rest_1_day": [
                row for row in rows if cls._number(row.get("rest_days")) == 1
            ],
            "rest_2_plus_days": [
                row for row in rows if cls._number(row.get("rest_days")) >= 2
            ],
        }
        opponents = {
            str(row.get("opponent_id"))
            for row in rows
            if row.get("opponent_id") not in (None, "")
        }
        for opponent in opponents:
            groups[f"opponent:{opponent}"] = [
                row for row in rows if str(row.get("opponent_id")) == opponent
            ]
        return {name: cls._averages(values) for name, values in groups.items()}

    @classmethod
    def _advanced(
        cls, rows: list[Mapping[str, Any]]
    ) -> AdvancedPlayerFeatures:
        baseline = cls._averages(rows)
        recent_three = cls._averages(rows[-3:])
        recent_five = cls._averages(rows[-5:])
        totals = {
            key: sum(cls._number(row.get(key)) for row in rows)
            for key in (
                "pts",
                "fgm",
                "fga",
                "fg3m",
                "fta",
                "turnovers",
                "team_field_goal_attempts",
                "team_free_throw_attempts",
                "team_turnovers",
            )
        }
        effective = (
            (totals["fgm"] + 0.5 * totals["fg3m"]) / totals["fga"]
            if totals["fga"]
            else 0.0
        )
        true_shooting_denominator = 2 * (
            totals["fga"] + 0.44 * totals["fta"]
        )
        true_shooting = (
            totals["pts"] / true_shooting_denominator
            if true_shooting_denominator
            else 0.0
        )
        player_possessions = (
            totals["fga"] + 0.44 * totals["fta"] + totals["turnovers"]
        )
        team_possessions = (
            totals["team_field_goal_attempts"]
            + 0.44 * totals["team_free_throw_attempts"]
            + totals["team_turnovers"]
        )
        usage = player_possessions / team_possessions if team_possessions else 0.0
        points = [cls._number(row.get("pts")) for row in rows]
        ewma = points[0] if points else 0.0
        for value in points[1:]:
            ewma = 0.4 * value + 0.6 * ewma
        volatility = cls._volatility(rows)
        trend_delta = recent_three.points - baseline.points
        normalized_trend = 0.5 + 0.5 * tanh(
            trend_delta / (volatility.standard_deviation + 1.0)
        )
        minutes_trend = recent_three.minutes - baseline.minutes
        recent_attempts = cls._mean(rows[-3:], "fga")
        baseline_attempts = cls._mean(rows, "fga")
        shot_trend = recent_attempts - baseline_attempts
        expected_minutes = 0.6 * recent_three.minutes + 0.4 * recent_five.minutes
        expected_usage = max(0.0, usage + max(0.0, shot_trend) / 100)
        role_stability = max(
            0.0,
            min(1.0, volatility.consistency_score / 100),
        )
        replacement = max(
            0.0,
            min(
                1.0,
                0.5 * max(0.0, minutes_trend) / 10
                + 0.3 * max(0.0, shot_trend) / 5
                + 0.2 * role_stability,
            ),
        )
        bench_rows = [row for row in rows if not bool(row.get("starter"))]
        bench_success = sum(
            cls._number(row.get("minutes")) < 24
            and cls._number(row.get("pts")) >= 10
            for row in bench_rows
        )
        bench_score = bench_success / len(bench_rows) if bench_rows else 0.0
        total_minutes = sum(cls._number(row.get("minutes")) for row in rows)
        return AdvancedPlayerFeatures(
            ewma_points=ewma,
            normalized_trend_score=max(0.0, min(1.0, normalized_trend)),
            effective_field_goal_percentage=effective,
            true_shooting_percentage=true_shooting,
            usage_rate=usage,
            minutes_trend=minutes_trend,
            shot_volume_trend=shot_trend,
            assist_opportunity=recent_three.assists - baseline.assists,
            rebound_opportunity=recent_three.rebounds - baseline.rebounds,
            turnover_pressure=(
                totals["turnovers"] / total_minutes if total_minutes else 0.0
            ),
            opponent_defensive_adjustment=cls._mean_optional(
                rows, "opponent_defensive_adjustment", 1.0
            ),
            pace_adjustment=cls._mean_optional(rows, "pace_adjustment", 1.0),
            role_stability=role_stability,
            expected_minutes=expected_minutes,
            expected_usage=expected_usage,
            replacement_opportunity=replacement,
            bench_opportunity_score=bench_score,
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
        if not rows:
            return 0.0
        return sum(cls._number(row.get(key)) for row in rows) / len(rows)

    @classmethod
    def _mean_optional(
        cls,
        rows: list[Mapping[str, Any]],
        key: str,
        neutral: float,
    ) -> float:
        values = [
            cls._number(row.get(key))
            for row in rows
            if row.get(key) not in (None, "")
        ]
        return sum(values) / len(values) if values else neutral

    @staticmethod
    def _qualitative_evidence(
        rows: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        evidence = []
        seen = set()
        for row in rows:
            claim = str(row.get("qualitative_context") or "").strip()
            source = str(row.get("qualitative_source") or "").strip()
            url = str(row.get("qualitative_url") or "").strip()
            if not claim or not source or not url:
                continue
            key = (claim, source, url)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                {
                    "claim": claim,
                    "source": source,
                    "url": url,
                    "evidence_type": "qualitative",
                    "confirmed_statistical_fact": False,
                }
            )
        return evidence

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
