from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sports.data.repositories.sqlite_lifecycle_repository import (
    SQLiteLifecycleRepository,
)
from sports.intelligence.models.lifecycle import (
    IntelligenceSituation,
    LifecycleStage,
    SituationEvent,
)


class IntelligenceLifecycleService:
    def __init__(self, repository: SQLiteLifecycleRepository) -> None:
        self.repository = repository

    def create_situation(
        self,
        *,
        situation_id: str,
        title: str,
        objective: str,
        observation: str,
        observed_at: str | None = None,
    ) -> IntelligenceSituation:
        if self.repository.list_events(situation_id):
            raise ValueError(f"Situation already exists: {situation_id}")
        self._append(
            situation_id,
            LifecycleStage.OBSERVE,
            "situation_created",
            {
                "title": title.strip(),
                "intelligence_objective": objective.strip(),
                "observation": observation.strip(),
            },
            observed_at,
        )
        situation = self.get_situation(situation_id)
        if situation is None:  # pragma: no cover - repository contract guard
            raise RuntimeError("Situation could not be reconstructed.")
        return situation

    def record_observation(
        self, situation_id: str, observation: str, *, occurred_at: str | None = None
    ) -> SituationEvent:
        return self._append(
            situation_id,
            LifecycleStage.OBSERVE,
            "observation_recorded",
            {"observation": observation.strip()},
            occurred_at,
        )

    def record_evidence(
        self,
        situation_id: str,
        evidence: dict[str, Any],
        *,
        occurred_at: str | None = None,
    ) -> SituationEvent:
        required = {"id", "claim", "source", "url", "retrieved_at"}
        missing = sorted(required - set(evidence))
        if missing:
            raise ValueError(f"Evidence is missing required fields: {', '.join(missing)}")
        return self._append(
            situation_id,
            LifecycleStage.RETRIEVE,
            "evidence_collected",
            {"evidence": dict(evidence)},
            occurred_at,
        )

    def record_evidence_gap(
        self, situation_id: str, gap: str, *, occurred_at: str | None = None
    ) -> SituationEvent:
        return self._append(
            situation_id,
            LifecycleStage.RETRIEVE,
            "evidence_gap_recorded",
            {"gap": gap.strip()},
            occurred_at,
        )

    def add_hypothesis(
        self,
        situation_id: str,
        hypothesis: dict[str, Any],
        *,
        occurred_at: str | None = None,
    ) -> SituationEvent:
        if not hypothesis.get("explanation"):
            raise ValueError("Hypothesis requires a proposed explanation.")
        return self._append(
            situation_id,
            LifecycleStage.ANALYZE,
            "hypothesis_added",
            {"hypothesis": dict(hypothesis)},
            occurred_at,
        )

    def evaluate_hypothesis(
        self,
        situation_id: str,
        hypothesis_id: str,
        *,
        confidence: float,
        status: str,
        indicator_results: dict[str, bool],
        supporting_evidence: list[str],
        contradicting_evidence: list[str],
        reason: str,
        occurred_at: str | None = None,
    ) -> SituationEvent:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1.")
        situation = self.get_situation(situation_id)
        if situation is None:
            raise KeyError(f"Unknown situation: {situation_id}")
        if not any(
            item.get("id") == hypothesis_id
            for item in situation.active_hypotheses
        ):
            raise KeyError(f"Unknown hypothesis: {hypothesis_id}")
        return self._append(
            situation_id,
            LifecycleStage.ANALYZE,
            "hypothesis_evaluated",
            {
                "hypothesis_id": hypothesis_id,
                "confidence": confidence,
                "status": status,
                "indicator_results": dict(indicator_results),
                "supporting_evidence": list(supporting_evidence),
                "contradicting_evidence": list(contradicting_evidence),
                "reason": reason,
            },
            occurred_at,
        )

    def add_forecast(
        self,
        situation_id: str,
        forecast: dict[str, Any],
        *,
        occurred_at: str | None = None,
    ) -> SituationEvent:
        for field in (
            "predicted_event",
            "probability",
            "baseline_probability",
            "horizon",
            "model_version",
        ):
            if field not in forecast:
                raise ValueError(f"Forecast requires {field}.")
        for field in ("probability", "baseline_probability"):
            value = float(forecast[field])
            if not 0 <= value <= 1:
                raise ValueError(f"{field} must be between 0 and 1.")
        return self._append(
            situation_id,
            LifecycleStage.FORECAST,
            "forecast_added",
            {"forecast": dict(forecast)},
            occurred_at,
        )

    def add_recommended_action(
        self, situation_id: str, action: str, *, occurred_at: str | None = None
    ) -> SituationEvent:
        return self._append(
            situation_id,
            LifecycleStage.FORECAST,
            "action_recommended",
            {"action": action.strip()},
            occurred_at,
        )

    def add_monitoring_rule(
        self,
        situation_id: str,
        rule: dict[str, Any],
        *,
        occurred_at: str | None = None,
    ) -> SituationEvent:
        return self._append(
            situation_id,
            LifecycleStage.MONITOR,
            "monitoring_rule_added",
            {"rule": dict(rule)},
            occurred_at,
        )

    def record_outcome(
        self,
        situation_id: str,
        outcome: dict[str, Any],
        *,
        occurred_at: str | None = None,
    ) -> SituationEvent:
        return self._append(
            situation_id,
            LifecycleStage.MONITOR,
            "outcome_recorded",
            {"outcome": dict(outcome)},
            occurred_at,
        )

    def record_lesson(
        self, situation_id: str, lesson: str, *, occurred_at: str | None = None
    ) -> SituationEvent:
        return self._append(
            situation_id,
            LifecycleStage.LEARN,
            "lesson_recorded",
            {"lesson": lesson.strip()},
            occurred_at,
        )

    def record_planner_activity(
        self,
        situation_id: str,
        activity: dict[str, Any],
        *,
        occurred_at: str | None = None,
    ) -> SituationEvent:
        return self._append(
            situation_id,
            LifecycleStage.MONITOR,
            "planner_activity_recorded",
            {"activity": dict(activity)},
            occurred_at,
        )

    def get_situation(self, situation_id: str) -> IntelligenceSituation | None:
        events = self.repository.list_events(situation_id)
        if not events:
            return None
        created = events[0].payload
        stage_order = {
            LifecycleStage.OBSERVE: 0,
            LifecycleStage.RETRIEVE: 1,
            LifecycleStage.ANALYZE: 2,
            LifecycleStage.FORECAST: 3,
            LifecycleStage.MONITOR: 4,
            LifecycleStage.LEARN: 5,
        }
        situation = IntelligenceSituation(
            id=situation_id,
            title=str(created["title"]),
            current_stage=max(events, key=lambda event: stage_order[event.stage]).stage,
            intelligence_objective=str(created["intelligence_objective"]),
            history=events,
        )
        for event in events:
            payload = event.payload
            if event.event_type in {"situation_created", "observation_recorded"}:
                situation.new_observations.append(str(payload["observation"]))
            elif event.event_type == "evidence_collected":
                situation.evidence_collected.append(dict(payload["evidence"]))
            elif event.event_type == "evidence_gap_recorded":
                situation.evidence_gaps.append(str(payload["gap"]))
            elif event.event_type == "hypothesis_added":
                situation.active_hypotheses.append(dict(payload["hypothesis"]))
            elif event.event_type == "hypothesis_evaluated":
                hypothesis = next(
                    item
                    for item in situation.active_hypotheses
                    if item.get("id") == payload["hypothesis_id"]
                )
                hypothesis["confidence"] = float(payload["confidence"])
                hypothesis["status"] = str(payload["status"])
                hypothesis["indicator_results"] = dict(
                    payload["indicator_results"]
                )
                hypothesis["supporting_evidence"] = list(
                    payload["supporting_evidence"]
                )
                hypothesis["contradicting_evidence"] = list(
                    payload["contradicting_evidence"]
                )
                hypothesis.setdefault("confidence_history", []).append(
                    {
                        "value": float(payload["confidence"]),
                        "reason": str(payload["reason"]),
                        "recorded_at": event.occurred_at,
                    }
                )
            elif event.event_type == "forecast_added":
                situation.forecasts.append(dict(payload["forecast"]))
            elif event.event_type == "action_recommended":
                situation.recommended_actions.append(str(payload["action"]))
            elif event.event_type == "monitoring_rule_added":
                situation.monitoring_rules.append(dict(payload["rule"]))
            elif event.event_type == "outcome_recorded":
                situation.final_outcome = dict(payload["outcome"])
            elif event.event_type == "lesson_recorded":
                situation.lessons_learned.append(str(payload["lesson"]))
        return situation

    def timeline(self, situation_id: str) -> list[dict[str, Any]]:
        descriptions = {
            "situation_created": "Situation observed",
            "observation_recorded": "New observation recorded",
            "evidence_collected": "Evidence retrieved",
            "evidence_gap_recorded": "Evidence gap identified",
            "hypothesis_added": "Hypothesis generated",
            "hypothesis_evaluated": "Hypothesis evaluated",
            "forecast_added": "Forecast created",
            "action_recommended": "Action recommended",
            "monitoring_rule_added": "Monitoring rule activated",
            "outcome_recorded": "Outcome recorded",
            "lesson_recorded": "Lesson recorded",
            "planner_activity_recorded": "Planner activity recorded",
        }
        return [
            {
                "sequence": event.sequence,
                "occurred_at": event.occurred_at,
                "stage": event.stage.value,
                "event_type": event.event_type,
                "description": descriptions[event.event_type],
                "payload": event.payload,
            }
            for event in self.repository.list_events(situation_id)
        ]

    def claim_graph(self, situation_id: str) -> dict[str, Any]:
        situation = self.get_situation(situation_id)
        if situation is None:
            raise KeyError(f"Unknown situation: {situation_id}")
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []

        def node(
            node_id: str,
            node_type: str,
            label: str,
            attributes: dict[str, Any] | None = None,
        ) -> None:
            nodes[node_id] = {
                "id": node_id,
                "type": node_type,
                "label": label,
                "attributes": attributes or {},
            }

        def edge(source: str, target: str, relationship: str) -> None:
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "relationship": relationship,
                }
            )

        evidence_claim_ids: dict[str, str] = {}
        for evidence in situation.evidence_collected:
            evidence_id = str(evidence["id"])
            source_id = f"source:{self._identifier(str(evidence['source']))}"
            evidence_node_id = f"evidence:{evidence_id}"
            claim_id = f"claim:{evidence_id}"
            node(
                source_id,
                "source",
                str(evidence["source"]),
                {
                    "url": evidence.get("url"),
                    "source_type": evidence.get("source_type", "unknown"),
                },
            )
            node(
                evidence_node_id,
                "evidence",
                str(evidence["claim"]),
                {
                    key: evidence.get(key)
                    for key in (
                        "reliability",
                        "freshness",
                        "independent_source_count",
                        "source_type",
                        "conflicting_reports",
                        "retrieved_at",
                    )
                },
            )
            node(claim_id, "claim", str(evidence["claim"]))
            edge(source_id, evidence_node_id, "derived_from")
            edge(evidence_node_id, claim_id, "supports")
            evidence_claim_ids[evidence_id] = claim_id

        for hypothesis in situation.active_hypotheses:
            hypothesis_id = str(
                hypothesis.get("id") or self._identifier(hypothesis["explanation"])
            )
            hypothesis_node_id = f"hypothesis:{hypothesis_id}"
            node(
                hypothesis_node_id,
                "hypothesis",
                str(hypothesis["explanation"]),
                {
                    key: hypothesis.get(key)
                    for key in (
                        "confidence",
                        "confidence_history",
                        "competing_explanations",
                        "expected_indicators",
                        "falsification_criteria",
                        "status",
                    )
                },
            )
            for evidence_id in hypothesis.get("supporting_evidence", []):
                claim_id = evidence_claim_ids.get(str(evidence_id))
                if claim_id:
                    edge(claim_id, hypothesis_node_id, "supports")
            for evidence_id in hypothesis.get("contradicting_evidence", []):
                claim_id = evidence_claim_ids.get(str(evidence_id))
                if claim_id:
                    edge(claim_id, hypothesis_node_id, "contradicts")

        forecast_node_ids: list[str] = []
        for forecast in situation.forecasts:
            forecast_id = str(
                forecast.get("id")
                or self._identifier(str(forecast["predicted_event"]))
            )
            forecast_node_id = f"forecast:{forecast_id}"
            forecast_node_ids.append(forecast_node_id)
            node(
                forecast_node_id,
                "forecast",
                str(forecast["predicted_event"]),
                {
                    key: forecast.get(key)
                    for key in (
                        "probability",
                        "baseline_probability",
                        "horizon",
                        "model_version",
                    )
                },
            )
            for hypothesis_id in forecast.get("hypothesis_ids", []):
                source_id = f"hypothesis:{hypothesis_id}"
                if source_id in nodes:
                    edge(source_id, forecast_node_id, "derived_from")

        for index, action in enumerate(situation.recommended_actions, start=1):
            action_id = f"decision:{index}"
            node(action_id, "decision", action)
            for forecast_id in forecast_node_ids:
                edge(forecast_id, action_id, "derived_from")

        if situation.final_outcome is not None:
            outcome = situation.final_outcome
            outcome_id = "outcome:final"
            node(outcome_id, "outcome", str(outcome.get("notes") or outcome))
            forecast_id = outcome.get("forecast_id")
            target = f"forecast:{forecast_id}" if forecast_id else None
            if target and target in nodes:
                relationship = (
                    "confirms" if bool(outcome.get("occurred")) else "contradicts"
                )
                edge(outcome_id, target, relationship)

        return {
            "situation_id": situation_id,
            "nodes": list(nodes.values()),
            "edges": edges,
            "missing_information": list(situation.evidence_gaps),
        }

    def evaluate_forecasts(self) -> dict[str, Any]:
        evaluated: list[tuple[dict[str, Any], bool]] = []
        outcome_by_forecast_id: dict[str, dict[str, Any]] = {}
        for situation_id in self.repository.list_situation_ids():
            situation = self.get_situation(situation_id)
            if situation is None or situation.final_outcome is None:
                continue
            forecast_id = situation.final_outcome.get("forecast_id")
            forecast = next(
                (
                    item
                    for item in situation.forecasts
                    if item.get("id") == forecast_id
                ),
                None,
            )
            if forecast is not None and "occurred" in situation.final_outcome:
                evaluated.append(
                    (forecast, bool(situation.final_outcome["occurred"]))
                )
                outcome_by_forecast_id[str(forecast.get("id"))] = dict(
                    situation.final_outcome
                )
        if not evaluated:
            return {
                "evaluated_forecasts": 0,
                "accuracy": 0.0,
                "brier_score": 0.0,
                "calibration_error": 0.0,
                "directional_accuracy": 0.0,
                "by_forecast_type": {},
                "by_model": {},
                "by_market": {},
                "by_confidence_range": {},
                "by_team": {},
                "by_player_role": {},
                "minutes_error": {"count": 0, "mean_absolute_error": 0.0},
                "usage_error": {"count": 0, "mean_absolute_error": 0.0},
                "market_direction_accuracy": 0.0,
                "confidence_bias": 0.0,
                "sample_size_warning": "No evaluated forecasts are available.",
                "feature_performance": {},
            }

        correct = [
            (float(forecast["probability"]) >= 0.5) == occurred
            for forecast, occurred in evaluated
        ]
        brier_values = [
            (float(forecast["probability"]) - float(occurred)) ** 2
            for forecast, occurred in evaluated
        ]
        predicted_average = sum(
            float(forecast["probability"]) for forecast, _ in evaluated
        ) / len(evaluated)
        observed_rate = sum(float(occurred) for _, occurred in evaluated) / len(
            evaluated
        )
        by_type: dict[str, dict[str, float | int]] = {}
        for forecast, occurred in evaluated:
            forecast_type = str(forecast["predicted_event"])
            group = by_type.setdefault(
                forecast_type,
                {"count": 0, "correct": 0, "brier_total": 0.0},
            )
            group["count"] = int(group["count"]) + 1
            is_correct = (float(forecast["probability"]) >= 0.5) == occurred
            group["correct"] = int(group["correct"]) + int(is_correct)
            group["brier_total"] = float(group["brier_total"]) + (
                float(forecast["probability"]) - float(occurred)
            ) ** 2
        for group in by_type.values():
            count = int(group["count"])
            group["accuracy"] = int(group.pop("correct")) / count
            group["brier_score"] = float(group.pop("brier_total")) / count

        accuracy = sum(correct) / len(correct)
        by_model = self._forecast_groups(
            evaluated, lambda forecast: str(forecast.get("model_version") or "unknown")
        )
        by_market = self._forecast_groups(
            evaluated, lambda forecast: str(forecast.get("market") or "unknown")
        )
        by_confidence = self._forecast_groups(
            evaluated,
            lambda forecast: self._confidence_bucket(
                float(forecast["probability"])
            ),
        )
        by_team = self._forecast_groups(
            evaluated, lambda forecast: str(forecast.get("team_id") or "unknown")
        )
        by_role = self._forecast_groups(
            evaluated,
            lambda forecast: str(forecast.get("player_role") or "unknown"),
        )
        minutes_errors = []
        usage_errors = []
        market_direction = []
        feature_values: dict[str, list[bool]] = {}
        for forecast, occurred in evaluated:
            outcome = outcome_by_forecast_id.get(str(forecast.get("id")))
            if outcome:
                if (
                    outcome.get("actual_minutes") is not None
                    and outcome.get("expected_minutes") is not None
                ):
                    minutes_errors.append(
                        abs(
                            float(outcome["actual_minutes"])
                            - float(outcome["expected_minutes"])
                        )
                    )
                if (
                    outcome.get("actual_usage_rate") is not None
                    and outcome.get("expected_usage_rate") is not None
                ):
                    usage_errors.append(
                        abs(
                            float(outcome["actual_usage_rate"])
                            - float(outcome["expected_usage_rate"])
                        )
                    )
                if outcome.get("market_moved_in_forecast_direction") is not None:
                    market_direction.append(
                        bool(outcome["market_moved_in_forecast_direction"])
                    )
            for factor in forecast.get("supporting_factors", []):
                feature_values.setdefault(str(factor), []).append(occurred)
        return {
            "evaluated_forecasts": len(evaluated),
            "accuracy": accuracy,
            "brier_score": sum(brier_values) / len(brier_values),
            "calibration_error": abs(predicted_average - observed_rate),
            "directional_accuracy": accuracy,
            "by_forecast_type": by_type,
            "by_model": by_model,
            "by_market": by_market,
            "by_confidence_range": by_confidence,
            "by_team": by_team,
            "by_player_role": by_role,
            "minutes_error": self._error_summary(minutes_errors),
            "usage_error": self._error_summary(usage_errors),
            "market_direction_accuracy": (
                sum(market_direction) / len(market_direction)
                if market_direction
                else 0.0
            ),
            "confidence_bias": predicted_average - observed_rate,
            "sample_size_warning": (
                f"Only {len(evaluated)} evaluated forecast(s); interpret metrics cautiously."
                if len(evaluated) < 30
                else ""
            ),
            "feature_performance": {
                factor: {
                    "count": len(values),
                    "success_rate": sum(values) / len(values),
                }
                for factor, values in feature_values.items()
            },
        }

    def build_research_memory(self) -> dict[str, Any]:
        lessons: list[dict[str, str]] = []
        successful_hypotheses: list[dict[str, Any]] = []
        failed_hypotheses: list[dict[str, Any]] = []
        source_values: dict[str, list[float]] = {}
        for situation_id in self.repository.list_situation_ids():
            situation = self.get_situation(situation_id)
            if situation is None:
                continue
            for lesson in situation.lessons_learned:
                lessons.append(
                    {"situation_id": situation_id, "lesson": lesson}
                )
            outcome = situation.final_outcome
            if outcome is not None and "occurred" in outcome:
                destination = (
                    successful_hypotheses
                    if bool(outcome["occurred"])
                    else failed_hypotheses
                )
                for hypothesis in situation.active_hypotheses:
                    destination.append(
                        {
                            "situation_id": situation_id,
                            "hypothesis_id": hypothesis.get("id"),
                            "explanation": hypothesis.get("explanation"),
                            "confidence": hypothesis.get("confidence"),
                        }
                    )
            for evidence in situation.evidence_collected:
                reliability = evidence.get("reliability")
                if reliability is None:
                    continue
                source_values.setdefault(str(evidence["source"]), []).append(
                    float(reliability)
                )
        source_reliability = {
            source: {
                "observations": len(values),
                "average_reliability": sum(values) / len(values),
            }
            for source, values in source_values.items()
        }
        return {
            "lessons": lessons,
            "successful_hypotheses": successful_hypotheses,
            "failed_hypotheses": failed_hypotheses,
            "source_reliability": source_reliability,
            "forecast_quality": self.evaluate_forecasts(),
        }

    def _append(
        self,
        situation_id: str,
        stage: LifecycleStage,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: str | None,
    ) -> SituationEvent:
        existing = self.repository.list_events(situation_id)
        if event_type != "situation_created" and not existing:
            raise KeyError(f"Unknown situation: {situation_id}")
        now = datetime.now(timezone.utc).isoformat()
        return self.repository.append(
            SituationEvent(
                id=None,
                situation_id=situation_id,
                sequence=0,
                occurred_at=occurred_at or now,
                recorded_at=now,
                stage=stage,
                event_type=event_type,
                payload=payload,
            )
        )

    @staticmethod
    def _forecast_groups(
        evaluated: list[tuple[dict[str, Any], bool]],
        key,
    ) -> dict[str, dict[str, float | int]]:
        groups: dict[str, list[tuple[float, bool]]] = {}
        for forecast, occurred in evaluated:
            groups.setdefault(key(forecast), []).append(
                (float(forecast["probability"]), occurred)
            )
        return {
            label: {
                "count": len(values),
                "accuracy": sum(
                    (probability >= 0.5) == occurred
                    for probability, occurred in values
                )
                / len(values),
                "brier_score": sum(
                    (probability - float(occurred)) ** 2
                    for probability, occurred in values
                )
                / len(values),
            }
            for label, values in groups.items()
        }

    @staticmethod
    def _confidence_bucket(probability: float) -> str:
        lower = int(probability * 10) * 10
        if lower == 100:
            lower = 90
        return f"{lower}-{lower + 9}%"

    @staticmethod
    def _error_summary(values: list[float]) -> dict[str, float | int]:
        return {
            "count": len(values),
            "mean_absolute_error": sum(values) / len(values) if values else 0.0,
        }

    @staticmethod
    def _identifier(value: str) -> str:
        return "-".join(
            token for token in "".join(
                character.lower() if character.isalnum() else " "
                for character in value
            ).split()
        )
