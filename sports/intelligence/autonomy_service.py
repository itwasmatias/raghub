from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from sports.data.repositories.sqlite_autonomy_repository import (
    SQLiteAutonomyRepository,
)
from sports.intelligence.models.autonomy import (
    AlertOutcome,
    IntelligenceAlert,
    ResearchLifecycle,
    Watch,
)
from sports.intelligence.models.player_intelligence import PlayerIntelligence


class AutonomyService:
    def __init__(self, repository: SQLiteAutonomyRepository) -> None:
        self.repository = repository

    def add_watch(
        self,
        target_type: str,
        target_value: str,
        condition: str,
    ) -> Watch:
        return self.repository.save_watch(
            Watch(None, target_type, target_value, condition)
        )

    def run_once(
        self,
        refresh: Callable[[], object],
        candidates: Callable[[], Iterable[PlayerIntelligence]],
    ) -> list[IntelligenceAlert]:
        refresh()
        profiles = list(candidates())
        alerts: list[IntelligenceAlert] = []
        for watch in self.repository.list_active_watches():
            for player in profiles:
                if not self._matches_target(watch, player):
                    continue
                signal = self._signal(watch.condition, player)
                if signal is None or watch.id is None:
                    continue
                baseline, observed, confidence = signal
                alerts.append(
                    self.repository.save_alert(
                        IntelligenceAlert(
                            id=None,
                            watch_id=watch.id,
                            player_id=player.player_id,
                            created_at=datetime.now(timezone.utc).isoformat(),
                            signal=watch.condition,
                            baseline_value=baseline,
                            observed_value=observed,
                            confidence=confidence,
                            evidence=player.evidence,
                        )
                    )
                )
        return alerts

    def evaluate(
        self,
        alert: IntelligenceAlert,
        later_value: float,
        later_minutes: float,
        notes: str = "",
    ) -> AlertOutcome:
        continued = later_value > alert.baseline_value
        role_grew = later_minutes > 0 and alert.signal in {
            "minutes_gain",
            "role_change",
        }
        correct = continued or role_grew
        outcome = AlertOutcome(
            alert_id=int(alert.id or 0),
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            continued=continued,
            role_grew=role_grew,
            correct=correct,
            confidence_error=abs(alert.confidence - float(correct)),
            notes=notes,
        )
        self.repository.save_outcome(outcome)
        return outcome

    def start_research(
        self,
        observation: str,
        hypothesis: str,
        experiment: str,
    ) -> ResearchLifecycle:
        return self.repository.save_lifecycle(
            ResearchLifecycle(
                None, observation, hypothesis, experiment, None, None
            )
        )

    def learn(
        self,
        lifecycle: ResearchLifecycle,
        outcome: str,
        learned_knowledge: str,
    ) -> ResearchLifecycle:
        return self.repository.save_lifecycle(
            ResearchLifecycle(
                lifecycle.id,
                lifecycle.observation,
                lifecycle.hypothesis,
                lifecycle.experiment,
                outcome,
                learned_knowledge,
            )
        )

    @staticmethod
    def _matches_target(watch: Watch, player: PlayerIntelligence) -> bool:
        if watch.target_type == "player":
            return player.player_id == watch.target_value
        if watch.target_type == "team":
            return player.latest_team == watch.target_value
        if watch.target_type == "condition":
            return True
        return False

    @staticmethod
    def _signal(
        condition: str,
        player: PlayerIntelligence,
    ) -> tuple[float, float, float] | None:
        profile = player.profiles.get("NBA:regular") or next(
            iter(player.profiles.values())
        )
        role = profile.role_change
        observed = profile.recent_five.points
        baseline = profile.baseline.points
        triggered = {
            "minutes_gain": role.minutes_change >= 5,
            "role_change": bool(role.starter_change or role.team_changed),
            "shooting_improvement": (
                profile.recent_five.field_goal_percentage
                > profile.baseline.field_goal_percentage + 0.05
            ),
            "bench_opportunity": role.short_appearance_success,
            "injury_return": role.injury_opportunity,
        }.get(condition, False)
        if not triggered:
            return None
        return (
            baseline,
            observed,
            profile.volatility.consistency_score / 100,
        )
