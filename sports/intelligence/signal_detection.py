from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WatchlistDefinition:
    id: str
    entities: tuple[str, ...]
    topics: tuple[str, ...]
    signals: tuple[str, ...]
    data_sources: tuple[str, ...]
    expected_update_frequency: str
    significance_threshold: float
    escalation_rules: tuple[str, ...]
    related_situation_ids: tuple[str, ...]
    triggered_actions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.entities or not self.signals:
            raise ValueError("watchlist id, entities, and signals are required")
        if self.significance_threshold < 0:
            raise ValueError("significance_threshold cannot be negative")


@dataclass(frozen=True, slots=True)
class SignalObservation:
    entity: str
    signal: str
    value: Any
    observed_at: str
    source: str


@dataclass(frozen=True, slots=True)
class SignalDetection:
    classification: str
    significant: bool
    reason: str
    related_situation_ids: tuple[str, ...]
    triggered_actions: tuple[str, ...] = ()


class SignalDetectionService:
    def evaluate(
        self,
        watchlist: WatchlistDefinition,
        observation: SignalObservation,
        history: list[SignalObservation],
    ) -> SignalDetection:
        if observation.entity not in watchlist.entities:
            raise ValueError("observation entity is outside the watchlist")
        if observation.signal not in watchlist.signals:
            raise ValueError("observation signal is outside the watchlist")
        comparable = [
            item
            for item in history
            if item.entity == observation.entity
            and item.signal == observation.signal
        ]
        if not comparable:
            return self._result(
                watchlist,
                "new_information",
                False,
                "No earlier matching observation exists.",
            )

        latest = comparable[-1]
        if observation.value == latest.value:
            if observation.source == latest.source:
                return self._result(
                    watchlist,
                    "repeated_reporting",
                    False,
                    "The same source repeated an unchanged value.",
                )
            return self._result(
                watchlist,
                "confirmation",
                False,
                "An independent source reported the same value.",
            )

        if isinstance(observation.value, bool) and isinstance(latest.value, bool):
            return self._result(
                watchlist,
                "contradiction",
                True,
                "The latest boolean status conflicts with the prior report.",
            )

        if self._numeric(observation.value) and self._numeric(latest.value):
            change = abs(float(observation.value) - float(latest.value))
            significant = change >= watchlist.significance_threshold
            return self._result(
                watchlist,
                "meaningful_change" if significant else "new_information",
                significant,
                f"Numeric value changed by {change:g}.",
            )

        return self._result(
            watchlist,
            "contradiction",
            True,
            "The latest categorical status conflicts with the prior report.",
        )

    @staticmethod
    def _numeric(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @staticmethod
    def _result(
        watchlist: WatchlistDefinition,
        classification: str,
        significant: bool,
        reason: str,
    ) -> SignalDetection:
        return SignalDetection(
            classification=classification,
            significant=significant,
            reason=reason,
            related_situation_ids=watchlist.related_situation_ids,
            triggered_actions=(
                watchlist.triggered_actions if significant else ()
            ),
        )

