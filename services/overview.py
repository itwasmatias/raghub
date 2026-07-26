from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


class OverviewService:
    SCHEMA_VERSION = "overview.v1"
    LEAGUES = ("WNBA", "MLB")

    def __init__(
        self,
        *,
        personal_service: Any,
        situation_room_service: Any,
        compute_repository: Any,
        clock=None,
    ) -> None:
        self.personal_service = personal_service
        self.situation_room_service = situation_room_service
        self.compute_repository = compute_repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def snapshot(self) -> dict[str, Any]:
        notices: list[dict[str, str]] = []

        try:
            sports_snapshot = self.personal_service.snapshot()
            sports = {
                league: self._league_payload(sports_snapshot, league, notices)
                for league in self.LEAGUES
            }
        except Exception as error:
            sports = {
                league: self._unavailable_league(league, "error", str(error))
                for league in self.LEAGUES
            }
            notices.append(
                {
                    "status": "error",
                    "scope": "sports",
                    "message": "Persisted Sports Intelligence state could not be read.",
                }
            )
            sports_snapshot = {}

        generated_at = str(
            sports_snapshot.get("generated_at")
            or self.clock().astimezone(timezone.utc).isoformat()
        )

        situation_room, evidence = self._situation_payload()
        compute = self._compute_payload()
        system_health = self._system_health(sports, situation_room, compute)
        overall_status = self._overall_status(sports, situation_room, evidence, compute)

        return {
            "schema_version": self.SCHEMA_VERSION,
            "generated_at": generated_at,
            "status": overall_status,
            "status_reason": (
                None
                if overall_status == "ready"
                else "One or more persisted data sections are incomplete or unavailable."
            ),
            "sports": sports,
            "situation_room": situation_room,
            "forecast_performance": {
                "status": self._performance_status(sports),
                "domains": [
                    {
                        "domain": league,
                        **deepcopy(sports[league]["performance"]),
                    }
                    for league in self.LEAGUES
                ],
                "detail_url": "/calibration",
            },
            "evidence": evidence,
            "system_health": system_health,
            "notices": notices,
        }

    def _league_payload(
        self,
        snapshot: dict[str, Any],
        league: str,
        notices: list[dict[str, str]],
    ) -> dict[str, Any]:
        feed = dict(snapshot.get("feed") or {})
        model = dict((snapshot.get("model") or {}).get("leagues", {}).get(league) or {})
        summary = dict((snapshot.get("league_summary") or {}).get(league) or {})
        feed_available = self._feed_available(feed)
        provider_status = self._provider_status(feed)
        freshness = self._freshness(feed)
        status = self._league_status(feed, freshness)

        events = [
            item
            for item in snapshot.get("events") or []
            if item.get("league") == league
        ]
        evaluations = [
            item
            for item in snapshot.get("evaluations") or []
            if str(item.get("canonical_event_id") or "")
            .lower()
            .startswith(f"{league.lower()}:")
        ]
        decisions_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in evaluations:
            decisions_by_event[str(item.get("canonical_event_id") or "")].append(
                self._decision(item)
            )

        predictions = [
            self._prediction_card(
                event,
                decisions_by_event.pop(str(event.get("canonical_id") or ""), []),
                freshness,
                model,
            )
            for event in events
        ]
        for event_id, decisions in decisions_by_event.items():
            predictions.append(
                {
                    "canonical_event_id": event_id,
                    "away_team": None,
                    "home_team": None,
                    "start_time": None,
                    "complete_books": None,
                    "quote_observed_at": None,
                    "freshness": freshness,
                    "model_version": model.get("version"),
                    "decisions": decisions,
                    "detail_url": f"/api/sip/games/{event_id}",
                }
            )

        performance = self._performance(snapshot, league)
        if status in {"stale", "error", "unavailable"}:
            notices.append(
                {
                    "status": status,
                    "scope": league,
                    "message": self._feed_message(feed, league, status),
                }
            )

        count_status = "available" if feed_available else "unavailable"
        return {
            "league": league,
            "status": status,
            "status_reason": self._feed_message(feed, league, status),
            "provider": {
                "name": str(
                    getattr(self.personal_service.settings, "odds_provider", "unknown")
                    or "unknown"
                ),
                "status": provider_status,
                "raw_status": feed.get("status"),
                "freshness": freshness,
                "as_of": feed.get("last_attempt_at"),
                "last_successful_refresh": feed.get("last_successful_refresh"),
                "error": feed.get("error"),
            },
            "model": {
                "status": model.get("calibration_status")
                or model.get("status")
                or "unavailable",
                "raw_status": model.get("status"),
                "version": model.get("version"),
                "detail": model.get("detail"),
            },
            "counts": {
                "upcoming_events": self._count(
                    summary, "upcoming_events", count_status
                ),
                "evaluations": self._count(summary, "evaluations", count_status),
                "qualified": self._count(summary, "qualified_choices", count_status),
                "no_bet": self._count(summary, "no_bet_results", count_status),
                "resolved_predictions": self._count(
                    summary, "resolved_predictions", "available"
                ),
            },
            "predictions": predictions,
            "performance": performance,
            "detail_url": "/wnba" if league == "WNBA" else "/#games",
        }

    @staticmethod
    def _decision(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "selection": item.get("selection"),
            "status": "qualified" if item.get("qualified") else "no_bet",
            "reason_codes": list(item.get("reason_codes") or []),
            "best_sportsbook": item.get("best_sportsbook"),
            "best_price": item.get("best_price"),
            "model_probability": item.get("model_probability"),
            "market_probability": item.get("market_probability"),
            "edge": item.get("edge"),
            "expected_value": item.get("expected_value"),
            "data_quality": item.get("data_quality"),
            "evaluated_at": item.get("evaluated_at"),
            "evidence": list(item.get("evidence") or []),
        }

    @staticmethod
    def _prediction_card(
        event: dict[str, Any],
        decisions: list[dict[str, Any]],
        freshness: str,
        model: dict[str, Any],
    ) -> dict[str, Any]:
        quote_times = [
            str(item.get("observed_at"))
            for item in event.get("quotes") or []
            if item.get("observed_at")
        ]
        forecasts = event.get("forecasts") or []
        return {
            "canonical_event_id": event.get("canonical_id"),
            "away_team": event.get("away_team_name"),
            "home_team": event.get("home_team_name"),
            "start_time": event.get("start_time"),
            "complete_books": event.get("complete_books"),
            "quote_observed_at": max(quote_times) if quote_times else None,
            "freshness": freshness,
            "model_version": (
                forecasts[0].get("model_version") if forecasts else model.get("version")
            ),
            "decisions": decisions,
            "detail_url": f"/api/sip/games/{event.get('canonical_id')}",
        }

    def _performance(self, snapshot: dict[str, Any], league: str) -> dict[str, Any]:
        rows = [
            item
            for item in snapshot.get("resolved_predictions") or []
            if str(item.get("league") or "").upper() == league
        ]
        if not rows:
            return {
                "status": "unavailable",
                "sample_size": 0,
                "accuracy": None,
                "brier_score": None,
                "log_loss": None,
                "model_versions": [],
                "through": None,
                "unavailable_reason": "No resolved predictions are available.",
            }

        sample_size = len(rows)
        return {
            "status": "available",
            "sample_size": sample_size,
            "accuracy": sum(bool(item.get("correct")) for item in rows) / sample_size,
            "brier_score": sum(
                float(item.get("brier_contribution") or 0.0) for item in rows
            )
            / sample_size,
            "log_loss": sum(
                float(item.get("log_loss_contribution") or 0.0) for item in rows
            )
            / sample_size,
            "model_versions": sorted(
                {
                    str(item.get("model_version"))
                    for item in rows
                    if item.get("model_version")
                }
            ),
            "through": max(
                (
                    str(item.get("resolved_at"))
                    for item in rows
                    if item.get("resolved_at")
                ),
                default=None,
            ),
            "unavailable_reason": None,
        }

    def _situation_payload(self) -> tuple[dict[str, Any], dict[str, Any]]:
        repository = self.situation_room_service.repository
        try:
            forecasts = repository.list_forecasts()
            evidence_rows = repository.list_evidence()
            latest_cycle = repository.get_latest_autonomy_cycle()
        except Exception:
            return (
                {
                    "status": "error",
                    "as_of": None,
                    "briefs": [],
                    "briefs_status": "error",
                    "active_forecasts": [],
                    "investigations": [],
                    "investigations_status": "unavailable",
                    "latest_autonomy_cycle": None,
                    "unavailable_reason": "Persisted Situation Room state could not be read.",
                    "detail_url": "/situation-room",
                },
                self._evidence_error(),
            )

        active_forecasts = [
            {
                "forecast_id": item.get("forecast_id"),
                "question": item.get("question"),
                "current_probability": item.get("current_probability"),
                "probability_change": item.get("probability_change"),
                "resolution_deadline": item.get("resolution_deadline"),
                "calibration_status": item.get("calibration_status"),
                "last_updated_at": item.get("last_updated_at"),
                "supporting_evidence_count": len(item.get("supporting_evidence") or []),
                "contradicting_evidence_count": len(
                    item.get("contradicting_evidence") or []
                ),
            }
            for item in forecasts
            if item.get("resolution_status", "open") == "open"
        ]
        last_times = [
            str(item.get("last_updated_at"))
            for item in forecasts
            if item.get("last_updated_at")
        ] + [
            str(item.get("observed_at"))
            for item in evidence_rows
            if item.get("observed_at")
        ]
        status = (
            "available" if forecasts or evidence_rows or latest_cycle else "unavailable"
        )
        return (
            {
                "status": status,
                "as_of": max(last_times) if last_times else None,
                "briefs": [],
                "briefs_status": "unavailable",
                "active_forecasts": active_forecasts,
                "investigations": [],
                "investigations_status": "unavailable",
                "latest_autonomy_cycle": latest_cycle,
                "unavailable_reason": (
                    None
                    if status == "available"
                    else "No persisted Situation Room records are available."
                ),
                "detail_url": "/situation-room",
            },
            self._evidence_payload(forecasts, evidence_rows),
        )

    @staticmethod
    def _evidence_payload(
        forecasts: list[dict[str, Any]], evidence_rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        supporting = sum(
            len(item.get("supporting_evidence") or []) for item in forecasts
        )
        contradicting = sum(
            len(item.get("contradicting_evidence") or []) for item in forecasts
        )
        return {
            "status": "available",
            "persisted_records": {
                "status": "available",
                "value": len(evidence_rows),
            },
            "supporting_links": {"status": "available", "value": supporting},
            "contradicting_links": {
                "status": "available",
                "value": contradicting,
            },
            "untracked_contradictions": {
                "status": "unavailable",
                "value": None,
            },
            "recent_records": [
                {
                    "evidence_id": item.get("evidence_id"),
                    "title": item.get("title"),
                    "source": item.get("source"),
                    "observed_at": item.get("observed_at"),
                }
                for item in evidence_rows[:5]
            ],
            "detail_url": "/situation-room/evidence",
        }

    @staticmethod
    def _evidence_error() -> dict[str, Any]:
        unavailable = {"status": "error", "value": None}
        return {
            "status": "error",
            "persisted_records": dict(unavailable),
            "supporting_links": dict(unavailable),
            "contradicting_links": dict(unavailable),
            "untracked_contradictions": {
                "status": "unavailable",
                "value": None,
            },
            "recent_records": [],
            "detail_url": "/situation-room/evidence",
        }

    def _compute_payload(self) -> dict[str, Any]:
        try:
            return dict(self.compute_repository.health() or {})
        except Exception:
            return {
                "status": "error",
                "error": "Persisted compute health could not be read.",
            }

    @staticmethod
    def _system_health(
        sports: dict[str, Any],
        situation_room: dict[str, Any],
        compute: dict[str, Any],
    ) -> dict[str, Any]:
        provider_by_name = {}
        for league, payload in sports.items():
            provider = payload.get("provider") or {}
            name = str(provider.get("name") or "unknown")
            entry = provider_by_name.setdefault(
                name,
                {
                    **provider,
                    "leagues": [],
                },
            )
            entry["leagues"].append(league)
        states = [
            *(item.get("status") for item in provider_by_name.values()),
            situation_room.get("status"),
            compute.get("status"),
        ]
        return {
            "status": (
                "error"
                if all(item == "error" for item in states)
                else "partial"
                if any(item in {"error", "unavailable", "stale"} for item in states)
                else "ready"
            ),
            "providers": list(provider_by_name.values()),
            "situation_room": {
                "status": situation_room.get("status"),
                "as_of": situation_room.get("as_of"),
            },
            "compute": compute,
        }

    @staticmethod
    def _count(summary: dict[str, Any], key: str, status: str) -> dict[str, Any]:
        if status != "available" or key not in summary:
            return {"status": "unavailable", "value": None}
        return {"status": "available", "value": int(summary[key])}

    @staticmethod
    def _feed_available(feed: dict[str, Any]) -> bool:
        return bool(
            feed.get("last_successful_refresh")
            or feed.get("status") in {"healthy", "degraded", "stale"}
        )

    @staticmethod
    def _freshness(feed: dict[str, Any]) -> str:
        value = str(feed.get("freshness") or "unknown").lower()
        if value in {"fresh", "stale"}:
            return value
        return "unknown" if value not in {"unavailable"} else "unavailable"

    @staticmethod
    def _provider_status(feed: dict[str, Any]) -> str:
        raw = str(feed.get("status") or "unavailable").lower()
        if raw == "healthy":
            return "ready"
        if raw in {"degraded", "error", "failed"}:
            return "error"
        if raw in {"not_refreshed", "unconfigured", "unavailable"}:
            return "unavailable"
        return "partial"

    @staticmethod
    def _league_status(feed: dict[str, Any], freshness: str) -> str:
        if freshness == "stale":
            return "stale"
        raw = str(feed.get("status") or "unavailable").lower()
        if raw == "healthy":
            return "ready"
        if raw in {"degraded", "error", "failed"}:
            return "error"
        if raw in {"not_refreshed", "unconfigured", "unavailable"}:
            return "unavailable"
        return "partial"

    @staticmethod
    def _feed_message(feed: dict[str, Any], league: str, status: str) -> str | None:
        if status == "ready":
            return None
        if feed.get("error"):
            return str(feed["error"])
        if status == "stale":
            return f"{league} data is persisted but stale."
        return f"{league} persisted provider data is {status}."

    @staticmethod
    def _performance_status(sports: dict[str, Any]) -> str:
        states = [item.get("performance", {}).get("status") for item in sports.values()]
        if all(item == "available" for item in states):
            return "available"
        if any(item == "available" for item in states):
            return "partial"
        return "unavailable"

    @staticmethod
    def _overall_status(
        sports: dict[str, Any],
        situation_room: dict[str, Any],
        evidence: dict[str, Any],
        compute: dict[str, Any],
    ) -> str:
        states = [
            *(item.get("status") for item in sports.values()),
            situation_room.get("status"),
            evidence.get("status"),
            compute.get("status"),
        ]
        if all(item in {"error", "unavailable"} for item in states):
            return "error"
        if any(item in {"error", "unavailable", "stale", "partial"} for item in states):
            return "partial"
        return "ready"

    @staticmethod
    def _unavailable_league(league: str, status: str, reason: str) -> dict[str, Any]:
        unavailable_count = {"status": "error", "value": None}
        return {
            "league": league,
            "status": status,
            "status_reason": reason,
            "provider": {
                "name": "unknown",
                "status": status,
                "raw_status": None,
                "freshness": "unknown",
                "as_of": None,
                "last_successful_refresh": None,
                "error": reason,
            },
            "model": {
                "status": "unavailable",
                "raw_status": None,
                "version": None,
                "detail": None,
            },
            "counts": {
                key: dict(unavailable_count)
                for key in (
                    "upcoming_events",
                    "evaluations",
                    "qualified",
                    "no_bet",
                    "resolved_predictions",
                )
            },
            "predictions": [],
            "performance": {
                "status": "unavailable",
                "sample_size": 0,
                "accuracy": None,
                "brier_score": None,
                "log_loss": None,
                "model_versions": [],
                "through": None,
                "unavailable_reason": reason,
            },
            "detail_url": "/wnba" if league == "WNBA" else "/#games",
        }
