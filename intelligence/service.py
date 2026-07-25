from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

import requests

from intelligence.models import (
    DecisionJournalEntry,
    ForecastModelScore,
    IntelligenceEvidence,
    IntelligenceGraphEdge,
    IntelligenceGraphNode,
    Investigation,
    MacroRegime,
    RiskDimension,
    RiskProfile,
    SituationAlert,
    SituationRoomSnapshot,
    Situation,
    UserImpactConclusion,
)
from intelligence.repository import SituationRoomRepository


class SituationRoomService:
    CACHE_TTL_SECONDS = 300
    GDELT_TTL_SECONDS = 900
    WATCHED_COMPANIES: dict[str, dict[str, str]] = {
        "AAPL": {
            "cik": "0000320193",
            "name": "Apple",
            "sector": "Consumer electronics",
        },
        "NVDA": {
            "cik": "0001045810",
            "name": "NVIDIA",
            "sector": "Semiconductors",
        },
        "LMT": {
            "cik": "0000936468",
            "name": "Lockheed Martin",
            "sector": "Defense contractors",
        },
        "UNH": {
            "cik": "0000731766",
            "name": "UnitedHealth Group",
            "sector": "Healthcare reimbursement",
        },
        "CAT": {
            "cik": "0000018230",
            "name": "Caterpillar",
            "sector": "Industrial equipment",
        },
    }
    COUNTRY_WATCHLIST: dict[str, str] = {
        "USA": "United States",
        "CHN": "China",
        "DEU": "Germany",
        "IND": "India",
    }
    WORLD_BANK_INDICATORS: dict[str, str] = {
        "gdp_growth": "NY.GDP.MKTP.KD.ZG",
        "inflation": "FP.CPI.TOTL.ZG",
        "trade_balance": "NE.RSB.GNFS.ZS",
        "current_account": "BN.CAB.XOKA.GD.ZS",
    }

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        ttl_seconds: int | None = None,
        fred_api_key: str | None = None,
        congress_api_key: str | None = None,
        api_data_gov_key: str | None = None,
        repository: SituationRoomRepository | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.ttl_seconds = ttl_seconds or self.CACHE_TTL_SECONDS
        self.fred_api_key = fred_api_key or os.getenv("FRED_API_KEY")
        self.api_data_gov_key = api_data_gov_key or os.getenv("API_DATA_GOV_KEY")
        self.congress_api_key = (
            congress_api_key
            or os.getenv("CONGRESS_API_KEY")
            or self.api_data_gov_key
        )
        self.repository = repository or SituationRoomRepository(
            os.getenv("RAGHUB_SITUATION_ROOM_DB", "data/situation_room.db")
        )
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._autonomy_lock = threading.Lock()
        self._autonomy_state: dict[str, Any] = {
            "enabled": False,
            "interval_seconds": 300,
            "last_cycle_at": None,
            "cycle_count": 0,
            "last_focus": "global",
            "last_query": "",
            "last_actions": [],
            "objective": "Monitor verified evidence for material changes and explain decision impact.",
            "activity": [],
        }

    def get_snapshot(
        self,
        *,
        focus: str = "global",
        query: str | None = None,
    ) -> dict[str, Any]:
        cache_key = (focus.lower(), (query or "").strip().lower())
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) <= self.ttl_seconds:
            return cached[1]

        source_status: list[dict[str, Any]] = []
        alerts: list[SituationAlert] = []
        government_actions: list[dict[str, Any]] = []
        opportunities: list[dict[str, Any]] = []
        risk_profiles: list[RiskProfile] = []

        macro_regime, macro_status = self._build_macro_regime()
        source_status.append(macro_status)
        if macro_regime is not None:
            alerts.append(self._macro_alert(macro_regime))

        treasury_alert, treasury_status = self._build_fiscal_monitor()
        source_status.append(treasury_status)
        if treasury_alert is not None:
            alerts.append(treasury_alert)

        agency_actions, agency_opportunities, agency_status = (
            self._build_spending_radar()
        )
        source_status.append(agency_status)
        government_actions.extend(agency_actions)
        opportunities.extend(agency_opportunities)
        alerts.extend(self._build_spending_alerts(agency_actions, agency_opportunities))

        sec_alerts, sec_profiles, sec_actions, sec_status = (
            self._build_corporate_engine()
        )
        source_status.append(sec_status)
        alerts.extend(sec_alerts)
        risk_profiles.extend(sec_profiles)
        government_actions.extend(sec_actions)

        country_profiles, country_status = self._build_country_risk_engine()
        source_status.append(country_status)
        risk_profiles.extend(country_profiles)

        congress_actions, congress_status = self._build_congress_actions()
        source_status.append(congress_status)
        government_actions.extend(congress_actions)
        alerts.extend(self._build_congress_alerts(congress_actions))

        public_events, gdelt_status = self._build_global_event_monitor(query or focus)
        source_status.append(gdelt_status)
        alerts.extend(public_events)

        bea_status = self._build_bea_status()
        source_status.append(bea_status)

        generated_at = datetime.now(timezone.utc).isoformat()
        visible_alerts = self._verified_alerts(alerts[:10], generated_at)
        active_forecasts = self._refresh_macro_forecast(
            macro_regime, visible_alerts, generated_at
        )
        graph_nodes, graph_edges = self._build_world_intelligence_graph(
            visible_alerts,
            government_actions,
            opportunities,
            risk_profiles,
        )
        investigations = self._build_investigations(
            visible_alerts, government_actions, opportunities
        )
        journal = self._build_decision_journal(visible_alerts)
        # Model capability descriptions are not forecast records and must not
        # be presented or counted as active predictions.
        tournament: list[ForecastModelScore] = []

        user_impacts = self._build_user_impact_conclusions(visible_alerts)
        situations = self._build_situations(
            visible_alerts, user_impacts, investigations, generated_at
        )
        primary_situation_id = situations[0].id if situations else ""
        self._attach_situation_ids(government_actions, visible_alerts, primary_situation_id)
        self._attach_situation_ids(opportunities, visible_alerts, primary_situation_id)
        for item in investigations:
            item.situation_id = item.situation_id or primary_situation_id
        for item in journal:
            item.situation_id = item.situation_id or primary_situation_id
        snapshot = SituationRoomSnapshot(
            generated_at=generated_at,
            alerts=visible_alerts,
            user_impact_conclusions=user_impacts,
            plain_language_summary=self._build_plain_language_summary(
                visible_alerts, user_impacts, macro_regime
            ),
            suggestion_prompts=self._build_suggestion_prompts(visible_alerts),
            situations=situations,
            primary_situation_id=primary_situation_id,
            data_integrity={
                "alert_count": len(visible_alerts),
                "evidence_unit_count": self.repository.count_evidence(),
                "graph_node_count": len(graph_nodes),
                "graph_edge_count": len(graph_edges),
                "source_record_count": sum(
                    item.get("status") == "live" for item in source_status
                ),
                "checked_at": generated_at,
            },
            graph_nodes=graph_nodes,
            graph_edges=graph_edges,
            graph_summary={
                "node_count": len(graph_nodes),
                "edge_count": len(graph_edges),
                "entity_types": sorted({node.node_type for node in graph_nodes}),
            },
            government_actions=government_actions[:10],
            opportunities=opportunities[:8],
            risk_profiles=risk_profiles[:8],
            macro_regime=macro_regime,
            investigations=investigations[:5],
            decision_journal=journal[:8],
            forecast_tournament=tournament,
            source_status=source_status,
        ).to_dict()
        snapshot["active_forecasts"] = active_forecasts
        snapshot["forecast_count"] = self.repository.count_forecasts()
        snapshot["intelligence_brief"] = self._build_intelligence_brief(
            visible_alerts, active_forecasts, generated_at
        )
        snapshot["watchlist_changes"] = self._build_watchlist_changes(
            visible_alerts, generated_at
        )
        snapshot["upcoming_events"] = self._build_upcoming_events(
            active_forecasts
        )
        snapshot["system_health"] = self._build_system_health(
            source_status, generated_at
        )
        snapshot["latest_autonomy_cycle"] = (
            self.repository.get_latest_autonomy_cycle()
        )
        snapshot["autonomy"] = self.get_autonomy_status()
        self._cache[cache_key] = (now, snapshot)
        return snapshot

    def get_situation(
        self, situation_id: str, *, focus: str = "global", query: str | None = None
    ) -> dict[str, Any] | None:
        snapshot = self.get_snapshot(focus=focus, query=query)
        situation = next(
            (
                item
                for item in snapshot.get("situations", [])
                if item.get("id") == situation_id
            ),
            None,
        )
        if situation is None:
            return None
        return {
            **situation,
            "brief_stages": [
                {"stage": "Observe", "content": situation["summary"]},
                {
                    "stage": "Retrieve",
                    "content": situation.get("evidence", []),
                },
                {
                    "stage": "Analyze",
                    "content": {
                        "entities": situation.get("entities", []),
                        "hypotheses": situation.get("hypotheses", []),
                    },
                },
                {"stage": "Forecast", "content": situation.get("forecasts", [])},
                {
                    "stage": "Recommend",
                    "content": situation.get("recommended_actions", []),
                },
                {
                    "stage": "Monitor",
                    "content": {
                        "investigations": situation.get("investigations", []),
                        "history": situation.get("history", []),
                    },
                },
                {"stage": "Learn", "content": situation.get("history", [])},
            ],
        }

    def _verified_alerts(
        self, alerts: list[SituationAlert], checked_at: str
    ) -> list[SituationAlert]:
        verified = []
        for alert in alerts:
            evidence = [
                item
                for item in alert.evidence_supports
                if item.title and item.source and item.url
            ]
            if not evidence:
                continue
            for item in evidence:
                if not item.observed_at:
                    item.observed_at = checked_at
                persisted = self.repository.upsert_evidence(item.to_dict())
                item.evidence_id = str(persisted["evidence_id"])
            alert.evidence_supports = evidence
            alert.situation_id = alert.alert_id
            verified.append(alert)
        return verified

    def _refresh_macro_forecast(
        self,
        regime: MacroRegime | None,
        alerts: list[SituationAlert],
        generated_at: str,
    ) -> list[dict[str, Any]]:
        if regime is None:
            return self.repository.list_forecasts()
        macro_alert = next(
            (item for item in alerts if "fred" in item.tags), None
        )
        if macro_alert is None or not macro_alert.evidence_supports:
            return self.repository.list_forecasts()
        evidence = macro_alert.evidence_supports[0]
        cpi_yoy = float(regime.indicators.get("cpi_yoy") or 0.0)
        forecast_id = "fred-cpi-above-3-next-quarter"
        existing = self.repository.get_forecast(forecast_id)
        created = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        deadline = (
            datetime.fromisoformat(
                str(existing["resolution_deadline"]).replace("Z", "+00:00")
            )
            if existing
            else created + timedelta(days=120)
        )
        current_probability = round(
            min(0.8, max(0.2, 0.5 + ((cpi_yoy - 3.0) * 0.12))), 2
        )
        condition = "at or above" if cpi_yoy >= 3.0 else "below"
        supporting = []
        contradicting = []
        evidence_reference = {
            "evidence_id": evidence.evidence_id,
            "title": evidence.title,
            "source": evidence.source,
            "url": evidence.url,
            "explanation": (
                f"The latest CPI observation is {cpi_yoy:.2f}% year over year."
            ),
        }
        if cpi_yoy >= 3.0:
            supporting.append(evidence_reference)
        else:
            contradicting.append(evidence_reference)
        forecast = {
            "forecast_id": forecast_id,
            "question": (
                "Will CPIAUCSL year-over-year inflation be at or above 3.0% "
                f"when this forecast resolves by {deadline.date().isoformat()}?"
            ),
            "domain": "macroeconomics",
            "outcome_options": ["Yes", "No"],
            "created_at": (
                existing.get("created_at") if existing else generated_at
            ),
            "resolution_deadline": deadline.isoformat(),
            "resolution_criteria": (
                "Resolve Yes if the latest CPIAUCSL observation available by "
                "the deadline is at least 3.0% above the observation from "
                "12 months earlier; otherwise resolve No."
            ),
            "resolution_source": {
                "name": "Federal Reserve Bank of St. Louis FRED CPIAUCSL",
                "url": "https://fred.stlouisfed.org/series/CPIAUCSL",
            },
            "prior_probability": 0.5,
            "current_probability": current_probability,
            "base_rate_explanation": (
                "Experimental persistence rule: the latest official annual CPI "
                "rate is compared with the 3% resolution threshold. No resolved "
                "forecast sample is yet available for calibration."
            ),
            "supporting_evidence": supporting,
            "contradicting_evidence": contradicting,
            "major_assumptions": [
                "FRED continues publishing CPIAUCSL without a series-definition change.",
                "Recent inflation persistence remains informative over the next quarter.",
            ],
            "update_triggers": [
                "A new CPIAUCSL observation is released.",
                "A material revision changes the latest year-over-year rate.",
            ],
            "calibration_status": "experimental",
            "calibration_note": (
                "Calibration unavailable: insufficient resolved outcomes"
            ),
            "model_version": "fred-cpi-persistence-v1",
            "resolution_status": "open",
            "why_probability_changed": (
                f"The latest official CPI rate is {cpi_yoy:.2f}%, {condition} "
                "the 3.0% resolution threshold."
            ),
            "increase_triggers": [
                "The next CPI release accelerates or remains above 3.0%.",
                "Monthly price gains broaden across major categories.",
            ],
            "decrease_triggers": [
                "The next CPI release falls below 3.0% year over year.",
                "Prior observations are revised materially lower.",
            ],
            "last_updated_at": generated_at,
            "forecast_horizon": "120 days",
            "situation_id": macro_alert.alert_id,
        }
        self.repository.upsert_forecast(forecast)
        return self.repository.list_forecasts()

    @staticmethod
    def _build_intelligence_brief(
        alerts: list[SituationAlert],
        forecasts: list[dict[str, Any]],
        generated_at: str,
    ) -> list[dict[str, Any]]:
        forecast_by_situation = {
            item.get("situation_id"): item for item in forecasts
        }
        developments = []
        seen_titles: set[str] = set()
        for alert in alerts:
            normalized_title = " ".join(alert.title.lower().split())
            if normalized_title in seen_titles:
                continue
            seen_titles.add(normalized_title)
            evidence = alert.evidence_supports
            if not evidence:
                continue
            timestamps = [item.observed_at for item in evidence if item.observed_at]
            forecast = forecast_by_situation.get(alert.alert_id)
            developments.append(
                {
                    "development_id": alert.alert_id,
                    "headline": alert.title,
                    "summary": alert.what_happened,
                    "why_it_matters": alert.why_it_matters,
                    "affected_entities": list(alert.who_is_exposed),
                    "evidence_count": len(evidence),
                    "evidence_ids": [item.evidence_id for item in evidence],
                    "primary_source": evidence[0].source,
                    "primary_source_url": evidence[0].url,
                    "source_diversity": len({item.source for item in evidence}),
                    "confidence": round(alert.confidence, 2),
                    "first_seen": min(timestamps) if timestamps else generated_at,
                    "last_meaningful_update": (
                        max(timestamps) if timestamps else generated_at
                    ),
                    "related_forecast_change": (
                        forecast.get("probability_change") if forecast else None
                    ),
                    "what_to_monitor_next": list(alert.invalidation_conditions),
                    "verification_status": "verified",
                }
            )
        return developments[:5]

    @staticmethod
    def _build_watchlist_changes(
        alerts: list[SituationAlert], generated_at: str
    ) -> list[dict[str, Any]]:
        watched = {
            "Apple": "Core corporate filing watchlist",
            "NVIDIA": "Semiconductor policy and supply-chain watchlist",
            "Lockheed Martin": "Government-spending concentration watchlist",
            "United States": "Macro and fiscal baseline watchlist",
            "China": "Trade and geopolitical-risk watchlist",
        }
        changes = []
        for alert in alerts:
            for entity in alert.who_is_exposed:
                reason = watched.get(entity)
                if not reason:
                    continue
                changes.append(
                    {
                        "entity": entity,
                        "development": alert.what_happened,
                        "importance": alert.severity,
                        "evidence": [
                            item.to_dict() for item in alert.evidence_supports
                        ],
                        "forecast_implication": alert.what_could_happen_next,
                        "last_checked": generated_at,
                        "watchlist_reason": reason,
                    }
                )
        return changes[:5]

    @staticmethod
    def _build_upcoming_events(
        forecasts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "event_id": f"resolution:{item['forecast_id']}",
                "title": f"Forecast resolution check: {item['question']}",
                "event_type": "government report",
                "scheduled_at": item["resolution_deadline"],
                "source": item["resolution_source"],
                "forecast_ids": [item["forecast_id"]],
            }
            for item in forecasts
            if item.get("resolution_status") == "open"
        ]

    @staticmethod
    def _build_system_health(
        source_status: list[dict[str, Any]], generated_at: str
    ) -> dict[str, Any]:
        stale = [
            item["source"]
            for item in source_status
            if item.get("status") not in {"live", "partial"}
        ]
        return {
            "connector_health": source_status,
            "last_successful_refresh": generated_at,
            "stale_sources": stale,
            "evidence_ingestion_status": "healthy",
            "forecast_engine_status": "experimental",
            "scheduler_status": "manual_or_autonomy_cycle",
            "recent_failures": [
                item for item in source_status if item.get("status") == "degraded"
            ],
        }

    @staticmethod
    def _attach_situation_ids(
        records: list[dict[str, Any]],
        alerts: list[SituationAlert],
        fallback_id: str,
    ) -> None:
        for record in records:
            record_text = " ".join(str(value) for value in record.values()).lower()
            match = next(
                (
                    alert
                    for alert in alerts
                    if alert.title.lower() in record_text
                    or any(
                        entity.lower() in record_text
                        for entity in alert.who_is_exposed
                        if entity
                    )
                ),
                None,
            )
            record["situation_id"] = match.alert_id if match else fallback_id

    @staticmethod
    def _build_situations(
        alerts: list[SituationAlert],
        impacts: list[UserImpactConclusion],
        investigations: list[Investigation],
        generated_at: str,
    ) -> list[Situation]:
        impact_by_id = {item.alert_id: item for item in impacts}
        situations = []
        for alert in alerts:
            linked_investigations = [
                item
                for item in investigations
                if alert.title in item.supporting_entities
                or any(
                    entity in item.supporting_entities
                    for entity in alert.who_is_exposed
                )
            ]
            for item in linked_investigations:
                item.situation_id = alert.alert_id
            impact = impact_by_id.get(alert.alert_id)
            situations.append(
                Situation(
                    id=alert.alert_id,
                    title=alert.title,
                    summary=f"{alert.what_happened} {alert.why_it_matters}".strip(),
                    status="active",
                    severity=alert.severity,
                    confidence=alert.confidence,
                    current_lifecycle_stage="monitor",
                    intelligence_objective=(
                        f"Determine whether {alert.what_could_happen_next} "
                        f"within {alert.forecast_horizon}."
                    ),
                    new_observations=[alert.what_happened],
                    evidence_collected=[
                        item.to_dict() for item in alert.evidence_supports
                    ],
                    evidence_gaps=[],
                    active_hypotheses=[
                        {
                            "situation_id": alert.alert_id,
                            "statement": alert.what_could_happen_next,
                            "status": "monitoring",
                            "confidence": alert.confidence,
                            "falsification_criteria": list(
                                alert.invalidation_conditions
                            ),
                        }
                    ],
                    evidence=list(alert.evidence_supports),
                    entities=[
                        {"id": f"entity:{SituationRoomService._slug(value)}", "name": value}
                        for value in alert.who_is_exposed
                    ],
                    forecasts=[
                        {
                            "situation_id": alert.alert_id,
                            "statement": alert.what_could_happen_next,
                            "probability": alert.confidence,
                            "horizon": alert.forecast_horizon,
                            "data_classification": "derived_forecast",
                            "invalidation_conditions": list(
                                alert.invalidation_conditions
                            ),
                        }
                    ],
                    hypotheses=[
                        {
                            "situation_id": alert.alert_id,
                            "statement": alert.what_could_happen_next,
                            "status": "monitoring",
                        }
                    ],
                    recommended_actions=[
                        {
                            "situation_id": alert.alert_id,
                            "action": impact.recommended_action if impact else "Monitor.",
                            "reason": impact.user_implication if impact else alert.why_it_matters,
                        }
                    ],
                    investigations=linked_investigations,
                    monitoring_rules=[
                        {
                            "situation_id": alert.alert_id,
                            "signal": condition,
                            "condition": "Escalate when observed",
                        }
                        for condition in alert.invalidation_conditions
                    ]
                    or [
                        {
                            "situation_id": alert.alert_id,
                            "signal": "material evidence change",
                            "condition": "Re-evaluate forecast",
                        }
                    ],
                    final_outcome=None,
                    lessons_learned=[],
                    history=[
                        {
                            "situation_id": alert.alert_id,
                            "sequence": 1,
                            "timestamp": generated_at,
                            "event_type": "situation_created",
                            "stage": "observe",
                            "status": "observed",
                            "summary": alert.what_happened,
                        }
                    ],
                )
            )
        return situations

    @staticmethod
    def _build_user_impact_conclusions(
        alerts: list[SituationAlert],
    ) -> list[UserImpactConclusion]:
        conclusions: list[UserImpactConclusion] = []
        for alert in alerts:
            impact_level = (
                "high"
                if alert.severity.lower() in {"critical", "high", "severe"}
                else "medium"
                if alert.severity.lower() in {"medium", "moderate", "warning"}
                else "low"
            )
            affected = [str(item) for item in alert.who_is_exposed if str(item)]
            affected_text = ", ".join(affected[:3]) or "the monitored entities"
            if impact_level == "high":
                implication = (
                    f"This is a high-priority change for decisions involving "
                    f"{affected_text}; acting without checking the next update "
                    "raises the risk of relying on a stale baseline."
                )
                action = (
                    f"Review current exposure to {affected_text}, inspect the linked "
                    "evidence, and delay irreversible decisions until the stated "
                    "invalidation conditions are checked."
                )
            elif impact_level == "medium":
                implication = (
                    f"This can change the risk/reward context for {affected_text}, "
                    "but the evidence does not justify an automatic decision."
                )
                action = (
                    f"Add {affected_text} to the watchlist, monitor the next source "
                    "refresh, and compare new evidence with the invalidation conditions."
                )
            else:
                implication = (
                    f"This is primarily a monitoring signal for {affected_text}; "
                    "it should inform context rather than trigger action by itself."
                )
                action = (
                    f"Keep {affected_text} under observation and reassess only if "
                    "severity, confidence, or corroborating evidence increases."
                )
            conclusions.append(
                UserImpactConclusion(
                    alert_id=alert.alert_id,
                    title=alert.title,
                    impact_level=impact_level,
                    affected_areas=affected,
                    observed_trigger=alert.what_happened,
                    direct_effect=alert.why_it_matters,
                    second_order_effect=alert.what_could_happen_next,
                    user_implication=implication,
                    recommended_action=action,
                    confidence=alert.confidence,
                    forecast_horizon=alert.forecast_horizon,
                    evidence_titles=[
                        item.title for item in alert.evidence_supports if item.title
                    ],
                    invalidation_conditions=list(alert.invalidation_conditions),
                )
            )
        return conclusions

    @staticmethod
    def _build_plain_language_summary(
        alerts: list[SituationAlert],
        impacts: list[UserImpactConclusion],
        macro_regime: MacroRegime | None,
    ) -> dict[str, Any]:
        high_count = sum(
            alert.severity.lower() in {"critical", "high", "severe"}
            for alert in alerts
        )
        if alerts:
            attention = (
                f"{high_count} need close attention."
                if high_count
                else "None are marked high priority right now."
            )
            headline = (
                f"The Situation Room found {len(alerts)} important updates. "
                f"{attention}"
            )
        else:
            headline = "No important updates were found in this scan."

        regime = macro_regime.regime if macro_regime is not None else "Unknown"
        return {
            "headline": headline,
            "current_context": (
                f"The current macro regime is {regime}. A macro regime is the "
                "overall pattern in inflation, jobs, growth, and interest rates."
            ),
            "in_plain_english": [
                (
                    f"{alert.title}: {alert.what_happened} "
                    f"In simple terms, it matters because {alert.why_it_matters}"
                )
                for alert in alerts[:3]
            ],
            "how_it_may_affect_you": [
                impact.user_implication for impact in impacts[:3]
            ],
            "key_concepts": [
                {
                    "term": "Confidence",
                    "meaning": (
                        "How strongly the available evidence supports a conclusion. "
                        "It is not a guarantee."
                    ),
                },
                {
                    "term": "Forecast horizon",
                    "meaning": "The period when the predicted effect may become visible.",
                },
                {
                    "term": "Exposure",
                    "meaning": (
                        "A person, company, industry, or part of the economy that "
                        "could feel the effect."
                    ),
                },
                {
                    "term": "Invalidation condition",
                    "meaning": (
                        "New evidence that would weaken or overturn the conclusion."
                    ),
                },
            ],
            "important_caveat": (
                "These are research conclusions from public data. Use them to ask "
                "better questions, not as automatic financial or life instructions."
            ),
        }

    @staticmethod
    def _build_suggestion_prompts(
        alerts: list[SituationAlert],
    ) -> list[dict[str, str]]:
        top = alerts[0] if alerts else None
        second = alerts[1] if len(alerts) > 1 else None
        prompts: list[dict[str, str]] = []
        if top is not None:
            prompts.extend(
                [
                    {
                        "label": "Explain the top story",
                        "prompt": (
                            f"Explain '{top.title}' in plain English for a young "
                            "adult and give one everyday example."
                        ),
                    },
                    {
                        "label": "Show my real-life impact",
                        "prompt": (
                            f"How could '{top.title}' affect jobs, prices, borrowing, "
                            "housing, or investments for an ordinary young adult?"
                        ),
                    },
                    {
                        "label": "Challenge the forecast",
                        "prompt": (
                            f"What evidence would make the forecast about "
                            f"'{top.title}' more or less likely?"
                        ),
                    },
                ]
            )
        if top is not None and second is not None:
            prompts.append(
                {
                    "label": "Connect the top stories",
                    "prompt": (
                        f"How are '{top.title}' and '{second.title}' connected, "
                        "and why could that connection matter to me?"
                    ),
                }
            )
        if not prompts:
            prompts.append(
                {
                    "label": "Start a simple scan",
                    "prompt": (
                        "Explain the current macro situation in plain English and "
                        "how it may affect a young adult."
                    ),
                }
            )
        return prompts

    def get_graph(
        self, *, focus: str = "global", query: str | None = None
    ) -> dict[str, Any]:
        snapshot = self.get_snapshot(focus=focus, query=query)
        nodes = [
            {
                **item,
                "score": round(float(item.get("score") or 0.0), 2),
                "score_definition": {
                    "name": "Evidence confidence",
                    "range": "0-100",
                    "meaning": "Higher values indicate stronger linked evidence.",
                    "main_contributing_factors": [
                        "source provenance",
                        "alert confidence",
                        "relationship strength",
                    ],
                },
            }
            for item in snapshot.get("graph_nodes") or []
            if float(item.get("score") or 0.0) > 0
        ]
        visible_ids = {item["node_id"] for item in nodes}
        edges = [
            {
                **item,
                "weight": round(float(item.get("weight") or 0.0), 2),
            }
            for item in snapshot.get("graph_edges") or []
            if item.get("source") in visible_ids
            and item.get("target") in visible_ids
            and item.get("evidence")
        ]
        return {
            "generated_at": snapshot.get("generated_at"),
            "summary": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "entity_types": sorted(
                    {str(item.get("node_type")) for item in nodes}
                ),
            },
            "nodes": nodes,
            "edges": edges,
        }

    def list_forecasts(self) -> dict[str, Any]:
        forecasts = self.repository.list_forecasts()
        return {
            "count": len(forecasts),
            "forecasts": forecasts,
            "empty_state": (
                None
                if forecasts
                else {
                    "title": "No active forecasts",
                    "why": (
                        "A forecast appears after authenticated official evidence "
                        "is retrieved and the forecast rule can define a deadline "
                        "and resolution condition."
                    ),
                    "next_action": "Refresh the Situation Room after configuring FRED.",
                }
            ),
        }

    def get_forecast(self, forecast_id: str) -> dict[str, Any] | None:
        forecast = self.repository.get_forecast(forecast_id)
        if forecast is None:
            return None
        forecast["probability_history"] = (
            self.repository.get_forecast_history(forecast_id)
        )
        return forecast

    def get_forecast_history(self, forecast_id: str) -> list[dict[str, Any]]:
        return self.repository.get_forecast_history(forecast_id)

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self.repository.get_evidence(evidence_id)

    def get_brief(
        self, *, focus: str = "global", query: str | None = None
    ) -> dict[str, Any]:
        snapshot = self.get_snapshot(focus=focus, query=query)
        developments = snapshot.get("intelligence_brief") or []
        return {
            "count": len(developments),
            "developments": developments,
            "empty_state": (
                None
                if developments
                else {
                    "title": "No verified developments",
                    "why": "No item passed the persisted-evidence integrity gate.",
                }
            ),
        }

    def get_watchlist_changes(
        self, *, focus: str = "global", query: str | None = None
    ) -> dict[str, Any]:
        changes = self.get_snapshot(focus=focus, query=query).get(
            "watchlist_changes"
        ) or []
        return {
            "count": len(changes),
            "changes": changes,
            "empty_state": (
                None
                if changes
                else {
                    "title": "No material watchlist changes",
                    "why": "Verified developments did not match configured watchlist entities.",
                }
            ),
        }

    def list_investigations(
        self, *, focus: str = "global", query: str | None = None
    ) -> dict[str, Any]:
        records = self.get_snapshot(focus=focus, query=query).get(
            "investigations"
        ) or []
        return {
            "count": len(records),
            "investigations": records,
            "empty_state": (
                None
                if records
                else {
                    "title": "No active investigations",
                    "why": "No verified situation currently requires an investigation.",
                }
            ),
        }

    def get_investigation(
        self, investigation_id: str, *, focus: str = "global"
    ) -> dict[str, Any] | None:
        records = self.list_investigations(focus=focus)["investigations"]
        return next(
            (
                item
                for item in records
                if item.get("investigation_id") == investigation_id
            ),
            None,
        )

    def get_system_health(
        self, *, focus: str = "global", query: str | None = None
    ) -> dict[str, Any]:
        snapshot = self.get_snapshot(focus=focus, query=query)
        return {
            **(snapshot.get("system_health") or {}),
            "record_counts": {
                "forecasts": self.repository.count_forecasts(),
                "evidence": self.repository.count_evidence(),
                "investigations": len(snapshot.get("investigations") or []),
            },
        }

    def get_prediction(
        self,
        *,
        focus: str = "global",
        query: str | None = None,
        scenario: str | None = None,
        horizon_days: int = 30,
        confidence_mode: str = "balanced",
    ) -> dict[str, Any]:
        normalized_focus = (focus or "global").strip() or "global"
        normalized_query = (query or "").strip()
        normalized_scenario = (scenario or normalized_query or normalized_focus).strip()
        normalized_mode = (confidence_mode or "balanced").strip().lower()
        bounded_horizon = max(7, min(int(horizon_days or 30), 365))

        snapshot = self.get_snapshot(
            focus=normalized_focus, query=normalized_query or None
        )
        alerts = snapshot.get("alerts") or []
        risks = snapshot.get("risk_profiles") or []
        impacts = snapshot.get("user_impact_conclusions") or []
        macro = snapshot.get("macro_regime") or {}

        alert_confidences = [
            float(alert.get("confidence") or 0.0)
            for alert in alerts
            if isinstance(alert, dict)
        ]
        mean_alert_confidence = (
            sum(alert_confidences) / len(alert_confidences)
            if alert_confidences
            else 0.62
        )

        risk_confidences = [
            float(profile.get("confidence") or 0.0)
            for profile in risks
            if isinstance(profile, dict)
        ]
        risk_pressure = (
            sum(risk_confidences) / len(risk_confidences) if risk_confidences else 0.5
        )

        scenario_text = normalized_scenario.lower()
        focus_text = normalized_focus.lower()

        shock_bias = 0.0
        if any(
            token in scenario_text
            for token in ["sanction", "conflict", "war", "shock", "crisis"]
        ):
            shock_bias -= 0.08
        if any(
            token in scenario_text
            for token in ["easing", "recovery", "productivity", "breakthrough"]
        ):
            shock_bias += 0.06
        if focus_text in {"macro", "policy"}:
            shock_bias -= 0.02
        if focus_text in {"corporate", "supply-chain"}:
            shock_bias += 0.02

        mode_bias = {
            "conservative": -0.06,
            "balanced": 0.0,
            "aggressive": 0.06,
        }.get(normalized_mode, 0.0)

        base_case_raw = (
            0.52 + (mean_alert_confidence - 0.62) * 0.35 + shock_bias + mode_bias
        )
        downside_raw = 0.26 + (risk_pressure - 0.5) * 0.4 - mode_bias * 0.45
        upside_raw = 1.0 - base_case_raw - downside_raw
        base_case_prob, downside_prob, upside_prob = self._normalize_probabilities(
            [base_case_raw, downside_raw, upside_raw]
        )

        uncertainty = max(
            0.08, min(0.4, 0.32 - mean_alert_confidence * 0.2 + risk_pressure * 0.18)
        )
        scenario_score = int(round(base_case_prob * 100))

        checkpoints = sorted(
            {round(bounded_horizon * index / 8) for index in range(9)}
        )
        path = []
        for day in checkpoints:
            t = day / bounded_horizon
            point_base, point_downside, point_upside = self._normalize_probabilities(
                [
                    base_case_prob - 0.02 * t,
                    downside_prob + 0.01 * t,
                    upside_prob + 0.015 * t,
                ]
            )
            interval = uncertainty * (0.5 + 0.5 * t) / 2
            path.append(
                {
                    "day": day,
                    "base_case": round(point_base, 3),
                    "upside": round(point_upside, 3),
                    "downside": round(point_downside, 3),
                    "base_low": round(max(0.0, point_base - interval), 3),
                    "base_high": round(min(1.0, point_base + interval), 3),
                }
            )

        key_drivers = []
        for alert in alerts[:3]:
            key_drivers.append(
                {
                    "title": str(alert.get("title") or "Alert signal"),
                    "category": str(alert.get("category") or "signal"),
                    "confidence": float(alert.get("confidence") or 0.0),
                    "direction": "risk"
                    if str(alert.get("severity") or "").lower()
                    in {"high", "critical", "severe"}
                    else "watch",
                }
            )

        suggested_actions = [
            {
                "action": str(
                    item.get("recommended_action") or "Monitor the next update window."
                ),
                "reason": str(item.get("user_implication") or item.get("title") or ""),
            }
            for item in impacts[:3]
        ]
        if not suggested_actions:
            suggested_actions = [
                {
                    "action": "Monitor cross-signal updates and rerun forecast on material change.",
                    "reason": "No impact conclusions were available for this cycle.",
                }
            ]

        regime_label = str(macro.get("regime") or "Unknown regime")
        summary = (
            f"{regime_label} context with {scenario_score}% base-case confidence over {bounded_horizon} days "
            f"for scenario '{normalized_scenario}'."
        )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "focus": normalized_focus,
            "query": normalized_query,
            "scenario": normalized_scenario,
            "horizon_days": bounded_horizon,
            "confidence_mode": normalized_mode,
            "summary": summary,
            "probabilities": {
                "base_case": round(base_case_prob, 3),
                "upside": round(upside_prob, 3),
                "downside": round(downside_prob, 3),
            },
            "uncertainty": round(uncertainty, 3),
            "macro_regime": {
                "regime": regime_label,
                "confidence": float(macro.get("confidence") or 0.0),
            },
            "forecast_path": path,
            "key_drivers": key_drivers,
            "suggested_actions": suggested_actions,
            "situation_id": snapshot.get("primary_situation_id") or "",
            "data_classification": "simulated_scenario",
        }

    def get_autonomy_status(self) -> dict[str, Any]:
        with self._autonomy_lock:
            return dict(self._autonomy_state)

    def set_autonomy(
        self,
        *,
        enabled: bool,
        interval_seconds: int | None = None,
        focus: str | None = None,
        query: str | None = None,
        objective: str | None = None,
    ) -> dict[str, Any]:
        with self._autonomy_lock:
            self._autonomy_state["enabled"] = bool(enabled)
            if interval_seconds is not None:
                self._autonomy_state["interval_seconds"] = max(
                    30, int(interval_seconds)
                )
            if focus is not None:
                self._autonomy_state["last_focus"] = (
                    focus or "global"
                ).strip() or "global"
            if query is not None:
                self._autonomy_state["last_query"] = (query or "").strip()
            if objective is not None and objective.strip():
                self._autonomy_state["objective"] = objective.strip()
            return dict(self._autonomy_state)

    def run_autonomy_cycle(
        self,
        *,
        focus: str = "global",
        query: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        status = self.get_autonomy_status()
        if not status.get("enabled") and not force:
            return {
                "status": "skipped",
                "reason": "Autonomy is disabled. Start autonomy or use force=true.",
                "autonomy": status,
                "actions": [],
            }

        started_at = datetime.now(timezone.utc).isoformat()
        cycle_id = f"cycle-{uuid4().hex[:16]}"
        snapshot = self.get_snapshot(focus=focus, query=query)
        actions = self._build_autonomy_actions(snapshot)
        now = datetime.now(timezone.utc).isoformat()
        forecasts = snapshot.get("active_forecasts") or []
        statuses = snapshot.get("source_status") or []
        findings = [
            {
                "title": item.get("title"),
                "summary": item.get("what_happened"),
                "evidence_count": len(item.get("evidence_supports") or []),
            }
            for item in (snapshot.get("alerts") or [])[:5]
        ]
        cycle = {
            "cycle_id": cycle_id,
            "objective": status.get("objective")
            or "Monitor verified evidence for material changes.",
            "objective_reason": (
                "This is the configured monitoring objective for the current "
                f"{focus or 'global'} Situation Room focus."
            ),
            "actions_attempted": actions,
            "connectors_queried": [
                {
                    "source": item.get("source"),
                    "status": item.get("status"),
                    "detail": item.get("detail"),
                }
                for item in statuses
            ],
            "evidence_found": sum(
                len(item.get("evidence_supports") or [])
                for item in snapshot.get("alerts") or []
            ),
            "findings": findings,
            "forecasts_considered": [
                item.get("forecast_id") for item in forecasts
            ],
            "forecasts_updated": [
                item.get("forecast_id")
                for item in forecasts
                if abs(float(item.get("probability_change") or 0.0)) > 0
            ],
            "open_questions": [
                trigger
                for item in forecasts
                for trigger in (item.get("update_triggers") or [])
            ][:8],
            "next_action": (
                actions[0]["title"]
                if actions
                else "Wait for the next scheduled official-data update."
            ),
            "cycle_status": "completed",
            "started_at": started_at,
            "completed_at": now,
            "errors": [
                {
                    "source": item.get("source"),
                    "detail": item.get("detail"),
                }
                for item in statuses
                if item.get("status") == "degraded"
            ],
        }
        self.repository.save_autonomy_cycle(cycle)
        with self._autonomy_lock:
            self._autonomy_state["last_cycle_at"] = now
            self._autonomy_state["cycle_count"] = (
                int(self._autonomy_state.get("cycle_count") or 0) + 1
            )
            self._autonomy_state["last_focus"] = (focus or "global").strip() or "global"
            self._autonomy_state["last_query"] = (query or "").strip()
            self._autonomy_state["last_actions"] = actions
            self._autonomy_state["activity"] = (
                [
                    {
                        "timestamp": now,
                        "objective": self._autonomy_state["objective"],
                        "result": f"Reviewed {len(snapshot.get('alerts') or [])} verified alerts and proposed {len(actions)} actions.",
                    }
                ]
                + list(self._autonomy_state.get("activity") or [])
            )[:20]
            updated_status = dict(self._autonomy_state)
        return {
            "status": "ok",
            "autonomy": updated_status,
            "actions": actions,
            "cycle": cycle,
            "snapshot_generated_at": snapshot.get("generated_at"),
        }

    def get_latest_autonomy_cycle(self) -> dict[str, Any]:
        cycle = self.repository.get_latest_autonomy_cycle()
        return cycle or {
            "status": "empty",
            "title": "No autonomous cycle has completed",
            "why": "Run a cycle after setting a monitoring objective.",
        }

    @staticmethod
    def _build_autonomy_actions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        alerts = snapshot.get("alerts") or []
        risks = snapshot.get("risk_profiles") or []
        statuses = snapshot.get("source_status") or []

        if alerts:
            top_alert = alerts[0]
            actions.append(
                {
                    "type": "escalate-alert",
                    "priority": str(top_alert.get("severity") or "medium").lower(),
                    "title": str(top_alert.get("title") or "Top alert"),
                    "reason": str(
                        top_alert.get("what_happened")
                        or top_alert.get("why_it_matters")
                        or "Primary alert should be reviewed."
                    ),
                }
            )

        if risks:
            top_risk = risks[0]
            actions.append(
                {
                    "type": "review-risk",
                    "priority": "medium",
                    "title": str(top_risk.get("subject") or "Top risk profile"),
                    "reason": str(
                        top_risk.get("summary")
                        or "Investigate elevated risk profile and map second-order effects."
                    ),
                }
            )

        degraded_sources = [
            status
            for status in statuses
            if str(status.get("status") or "").lower()
            in {"degraded", "partial", "unconfigured"}
        ]
        if degraded_sources:
            source_names = ", ".join(
                str(item.get("source") or "unknown") for item in degraded_sources[:3]
            )
            actions.append(
                {
                    "type": "repair-sources",
                    "priority": "high",
                    "title": "Source reliability remediation",
                    "reason": f"Address degraded or partial feeds: {source_names}.",
                }
            )

        if not actions:
            actions.append(
                {
                    "type": "monitor",
                    "priority": "low",
                    "title": "Continue baseline monitoring",
                    "reason": "No urgent cross-signal anomalies detected in this cycle.",
                }
            )
        return actions

    def _build_macro_regime(self) -> tuple[MacroRegime | None, dict[str, Any]]:
        if not self.fred_api_key:
            return None, self._source_status(
                "fred",
                "unconfigured",
                "FRED support is implemented but requires FRED_API_KEY in the environment.",
            )
        try:
            cpi = self._fetch_fred_series("CPIAUCSL")
            unemployment = self._fetch_fred_series("UNRATE")
            fedfunds = self._fetch_fred_series("FEDFUNDS")
        except Exception as error:
            return None, self._source_status("fred", "degraded", str(error))

        if len(cpi) < 13 or len(unemployment) < 13 or len(fedfunds) < 2:
            return None, self._source_status(
                "fred", "degraded", "Insufficient macro history returned."
            )

        latest_cpi = cpi[-1][1]
        prior_cpi = cpi[-13][1]
        yoy_inflation = ((latest_cpi / prior_cpi) - 1) * 100 if prior_cpi else 0.0
        unemployment_change = unemployment[-1][1] - unemployment[-13][1]
        policy_rate = fedfunds[-1][1]

        if yoy_inflation >= 3.0 and policy_rate >= 4.0 and unemployment_change <= 0.3:
            regime = "Inflationary growth"
            confidence = 0.78
            summary = "Inflation remains elevated while policy is still restrictive and labor weakness has not yet broken the cycle."
        elif unemployment_change >= 0.5 and yoy_inflation <= 3.0:
            regime = "Slowdown / recession risk"
            confidence = 0.74
            summary = "Labor slack is rising while price pressure is easing, a combination associated with slower growth and recession sensitivity."
        elif policy_rate < 2.5 and yoy_inflation < 2.5:
            regime = "Policy easing / disinflation"
            confidence = 0.7
            summary = "Rates are no longer highly restrictive and inflation has cooled, pointing toward a disinflationary easing regime."
        else:
            regime = "Mixed late-cycle regime"
            confidence = 0.62
            summary = "Signals are mixed: policy remains consequential, inflation is not fully normalized, and labor conditions are no longer uniformly strong."

        return (
            MacroRegime(
                regime=regime,
                confidence=confidence,
                summary=summary,
                indicators={
                    "cpi_yoy": round(yoy_inflation, 2),
                    "unemployment_change_yoy": round(unemployment_change, 2),
                    "fed_funds": round(policy_rate, 2),
                    "cpi_observation_date": cpi[-1][0],
                    "unemployment_observation_date": unemployment[-1][0],
                    "fed_funds_observation_date": fedfunds[-1][0],
                },
            ),
            self._source_status(
                "fred", "live", "Macro regime indicators refreshed from FRED."
            ),
        )

    def _build_fiscal_monitor(self) -> tuple[SituationAlert | None, dict[str, Any]]:
        try:
            payload = self._fetch_json(
                "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny?sort=-record_date&page[size]=16"
            )
        except Exception as error:
            return None, self._source_status("treasury", "degraded", str(error))

        rows = payload.get("data") or []
        if len(rows) < 2:
            return None, self._source_status(
                "treasury",
                "degraded",
                "Treasury data did not include enough observations.",
            )

        latest = rows[0]
        prior = rows[-1]
        latest_debt = float(latest.get("tot_pub_debt_out_amt") or 0.0)
        prior_debt = float(prior.get("tot_pub_debt_out_amt") or 0.0)
        growth = ((latest_debt / prior_debt) - 1) * 100 if prior_debt else 0.0
        severity = "high" if growth >= 6 else "medium" if growth >= 3 else "moderate"
        alert = SituationAlert(
            alert_id="treasury-fiscal-sustainability",
            category="fiscal-sustainability",
            severity=severity,
            title="Federal debt load and refinancing sensitivity remain in focus",
            what_happened=(
                f"Treasury debt to the penny reached approximately ${latest_debt / 1_000_000_000_000:.1f}T, "
                f"up {growth:.1f}% versus the comparison window returned by Treasury Fiscal Data."
            ),
            why_it_matters="Higher debt levels increase sensitivity to interest costs, issuance pressure, and future policy tradeoffs across public and private capital markets.",
            who_is_exposed=[
                "U.S. Treasury market",
                "rate-sensitive sectors",
                "long-duration equities",
                "federal budget planners",
            ],
            what_could_happen_next="If issuance and interest expense continue to outpace revenue growth, long-duration funding stress and fiscal crowd-out risk can move higher.",
            evidence_supports=[
                IntelligenceEvidence(
                    title="Treasury Fiscal Data: Debt to the Penny",
                    source="Treasury Fiscal Data",
                    summary=f"Latest record date {latest.get('record_date')} with total public debt outstanding near ${latest_debt / 1_000_000_000_000:.1f}T.",
                    url="https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/debt-to-the-penny",
                    observed_at=str(latest.get("record_date") or ""),
                )
            ],
            invalidation_conditions=[
                "Revenue growth accelerates faster than debt-service growth.",
                "Treasury issuance pressure eases without a concurrent refinancing spike.",
            ],
            confidence=0.78,
            forecast_horizon="2-6 quarters",
            tags=["debt", "rates", "fiscal"],
        )
        return alert, self._source_status(
            "treasury", "live", "Fiscal sustainability series refreshed."
        )

    def _build_spending_radar(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        try:
            recipient_payload = self._fetch_json(
                "https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/?limit=6&page=1",
                method="POST",
                payload={
                    "filters": {
                        "time_period": [
                            {"start_date": "2025-01-01", "end_date": "2025-12-31"}
                        ]
                    },
                    "category": "recipient",
                },
            )
            agency_payload = self._fetch_json(
                "https://api.usaspending.gov/api/v2/search/spending_by_category/awarding_agency/?limit=5&page=1",
                method="POST",
                payload={
                    "filters": {
                        "time_period": [
                            {"start_date": "2025-01-01", "end_date": "2025-12-31"}
                        ]
                    },
                    "category": "awarding_agency",
                },
            )
        except Exception as error:
            return [], [], self._source_status("usaspending", "degraded", str(error))

        actions = []
        for item in (agency_payload.get("results") or [])[:4]:
            name = str(item.get("name") or "Unknown agency")
            amount = float(item.get("amount") or 0.0)
            actions.append(
                {
                    "title": f"{name} remains a major federal spending channel",
                    "agency": name,
                    "amount": amount,
                    "impact": "Large budget execution can propagate through contractors, labor markets, and regional supply chains.",
                    "timing": "current fiscal year",
                    "analogues": [
                        "prior procurement cycles",
                        "sector concentration waves",
                    ],
                    "evidence": f"USAspending awarding-agency totals indicate roughly ${amount / 1_000_000_000:.1f}B in tracked spending.",
                }
            )

        opportunities = []
        for item in (recipient_payload.get("results") or [])[:5]:
            name = str(item.get("name") or "Unknown recipient")
            amount = float(item.get("amount") or 0.0)
            opportunities.append(
                {
                    "title": name,
                    "amount": amount,
                    "why": "Federal award concentration can signal sustained demand and follow-on contract opportunity.",
                    "next": "Check whether the recipient cluster points to a repeatable technology or procurement theme.",
                }
            )
        return (
            actions,
            opportunities,
            self._source_status(
                "usaspending",
                "live",
                "Federal award and agency spending radar refreshed.",
            ),
        )

    def _build_corporate_engine(
        self,
    ) -> tuple[
        list[SituationAlert], list[RiskProfile], list[dict[str, Any]], dict[str, Any]
    ]:
        alerts: list[SituationAlert] = []
        profiles: list[RiskProfile] = []
        actions: list[dict[str, Any]] = []
        errors: list[str] = []
        for ticker, metadata in self.WATCHED_COMPANIES.items():
            try:
                submissions = self._fetch_json(
                    f"https://data.sec.gov/submissions/CIK{metadata['cik']}.json"
                )
                facts = self._fetch_json(
                    f"https://data.sec.gov/api/xbrl/companyfacts/CIK{metadata['cik']}.json"
                )
            except Exception as error:
                errors.append(f"{ticker}: {error}")
                continue

            recent = submissions.get("filings", {}).get("recent", {})
            forms = recent.get("form") or []
            dates = recent.get("filingDate") or []
            latest_form = str(forms[0] if forms else "No recent filing")
            latest_date = str(dates[0] if dates else "")
            cash = self._latest_company_fact(
                facts,
                "us-gaap",
                [
                    "CashAndCashEquivalentsAtCarryingValue",
                    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                ],
            )
            debt = self._latest_company_fact(
                facts,
                "us-gaap",
                [
                    "LongTermDebtNoncurrent",
                    "LongTermDebtAndCapitalLeaseObligations",
                    "LongTermDebt",
                ],
            )
            revenue = self._latest_company_fact(
                facts,
                "us-gaap",
                ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
            )
            cash_to_debt = (cash / debt) if debt else 2.0
            liquidity_score = (
                85 if cash_to_debt >= 1 else 60 if cash_to_debt >= 0.5 else 35
            )
            leverage_score = (
                80 if debt <= revenue * 0.4 else 55 if debt <= revenue * 0.8 else 30
            )

            alerts.append(
                SituationAlert(
                    alert_id=f"sec-{ticker.lower()}",
                    category="corporate-early-warning",
                    severity="medium" if leverage_score < 60 else "moderate",
                    title=f"{metadata['name']} filing monitor: {latest_form} on {latest_date or 'recent date unavailable'}",
                    what_happened=(
                        f"SEC EDGAR shows a recent {latest_form} filing for {metadata['name']} with cash near ${cash / 1_000_000_000:.1f}B "
                        f"and debt near ${debt / 1_000_000_000:.1f}B based on the latest XBRL values available."
                    ),
                    why_it_matters="Changes in cash, debt, and filing cadence can signal financing pressure, shifting management posture, or heightened disclosure risk.",
                    who_is_exposed=[
                        metadata["name"],
                        metadata["sector"],
                        "credit investors",
                        "equity holders",
                    ],
                    what_could_happen_next="If leverage rises faster than operating performance, the filing trail can start to confirm refinancing or margin pressure.",
                    evidence_supports=[
                        IntelligenceEvidence(
                            title=f"SEC submissions for {metadata['name']}",
                            source="SEC EDGAR",
                            summary=f"Latest tracked form {latest_form} on {latest_date or 'unknown date'}.",
                            url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={metadata['cik']}",
                            observed_at=latest_date,
                        )
                    ],
                    invalidation_conditions=[
                        "Cash generation improves while debt growth stabilizes.",
                        "Subsequent filings show de-leveraging or stronger operating cash conversion.",
                    ],
                    confidence=0.7,
                    forecast_horizon="1-4 quarters",
                    tags=[ticker, "sec", metadata["sector"].lower()],
                )
            )
            profiles.append(
                RiskProfile(
                    subject=metadata["name"],
                    subject_type="company",
                    summary=f"Financial resilience profile for {metadata['name']} using latest SEC XBRL balances.",
                    confidence=0.72,
                    dimensions=[
                        RiskDimension(
                            label="Liquidity",
                            score=float(liquidity_score),
                            status="High"
                            if liquidity_score >= 75
                            else "Moderate"
                            if liquidity_score >= 50
                            else "Low",
                            rationale=f"Cash-to-debt ratio is approximately {cash_to_debt:.2f}.",
                        ),
                        RiskDimension(
                            label="Leverage",
                            score=float(leverage_score),
                            status="High"
                            if leverage_score >= 75
                            else "Moderate"
                            if leverage_score >= 50
                            else "Low",
                            rationale=f"Debt relative to latest revenue estimate is approximately {debt / revenue:.2f}.",
                        ),
                        RiskDimension(
                            label="Government Dependency",
                            score=58.0 if ticker == "LMT" else 46.0,
                            status="Moderate",
                            rationale="Defense and regulated sectors remain more directly exposed to policy and procurement changes.",
                        ),
                    ],
                )
            )
            actions.append(
                {
                    "title": f"{metadata['name']} policy exposure",
                    "sector": metadata["sector"],
                    "companies": [metadata["name"]],
                    "timing": "next filing cycle",
                    "historical_analogues": [
                        "past disclosure shifts",
                        "balance-sheet deterioration",
                    ],
                    "market_reaction_window": "before and after filing date",
                }
            )
        status = self._source_status(
            "sec",
            "live" if not errors else "partial",
            "SEC EDGAR filing and XBRL monitors refreshed."
            if not errors
            else "; ".join(errors[:3]),
        )
        return alerts, profiles, actions, status

    def _build_country_risk_engine(self) -> tuple[list[RiskProfile], dict[str, Any]]:
        profiles: list[RiskProfile] = []
        errors: list[str] = []
        for code, name in self.COUNTRY_WATCHLIST.items():
            try:
                indicator_values = {
                    label: self._fetch_world_bank_indicator(code, indicator)
                    for label, indicator in self.WORLD_BANK_INDICATORS.items()
                }
            except Exception as error:
                errors.append(f"{code}: {error}")
                continue

            inflation = indicator_values.get("inflation") or 0.0
            growth = indicator_values.get("gdp_growth") or 0.0
            current_account = indicator_values.get("current_account") or 0.0
            fiscal_score = (
                70.0
                if current_account >= -2
                else 48.0
                if current_account >= -5
                else 32.0
            )
            economic_score = 78.0 if growth >= 2 else 52.0 if growth >= 0 else 30.0
            currency_score = (
                68.0 if inflation <= 4 else 50.0 if inflation <= 7 else 28.0
            )
            profiles.append(
                RiskProfile(
                    subject=name,
                    subject_type="country",
                    summary=f"Dynamic country-risk profile for {name} using World Bank indicator history.",
                    confidence=0.68,
                    dimensions=[
                        RiskDimension(
                            label="Economic Stability",
                            score=economic_score,
                            status="High"
                            if economic_score >= 70
                            else "Moderate"
                            if economic_score >= 45
                            else "Low",
                            rationale=f"Latest GDP growth estimate is {growth:.1f}%.",
                        ),
                        RiskDimension(
                            label="Fiscal Risk",
                            score=fiscal_score,
                            status="High"
                            if fiscal_score >= 70
                            else "Moderate"
                            if fiscal_score >= 45
                            else "Low",
                            rationale=f"Current-account balance indicator is {current_account:.1f}% of GDP.",
                        ),
                        RiskDimension(
                            label="Currency Risk",
                            score=currency_score,
                            status="High"
                            if currency_score >= 70
                            else "Moderate"
                            if currency_score >= 45
                            else "Low",
                            rationale=f"Inflation indicator is {inflation:.1f}%.",
                        ),
                    ],
                )
            )
        status = self._source_status(
            "world-bank",
            "live" if not errors else "partial",
            "Country-risk profiles refreshed from World Bank indicators."
            if not errors
            else "; ".join(errors[:3]),
        )
        return profiles, status

    def _build_congress_actions(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.congress_api_key:
            return [], self._source_status(
                "congress",
                "unconfigured",
                "Congress.gov support requires API_DATA_GOV_KEY (or the legacy CONGRESS_API_KEY alias) in the environment.",
            )
        try:
            payload = self._fetch_provider_json(
                "https://api.congress.gov/v3/bill",
                provider="Congress.gov",
                params={
                    "limit": 5,
                    "format": "json",
                    "api_key": self.congress_api_key,
                },
            )
        except Exception as error:
            return [], self._source_status("congress", "degraded", str(error))
        actions = []
        for item in (payload.get("bills") or [])[:5]:
            title = str(
                item.get("title")
                or item.get("latestAction", {}).get("text")
                or "Congressional action"
            )
            actions.append(
                {
                    "title": title,
                    "sector": "government policy",
                    "companies": [],
                    "timing": str(
                        (item.get("latestAction") or {}).get("actionDate") or "pending"
                    ),
                    "historical_analogues": ["prior bill cycle"],
                    "market_reaction_window": "around legislative milestones",
                }
            )
        return actions, self._source_status(
            "congress", "live", "Congress.gov legislative feed refreshed."
        )

    def _build_global_event_monitor(
        self, query: str
    ) -> tuple[list[SituationAlert], dict[str, Any]]:
        try:
            payload = self._fetch_json(
                "https://api.gdeltproject.org/api/v2/doc/doc?"
                f"query={requests.utils.quote(query or 'geopolitics')}"
                "&mode=artlist&maxrecords=3&format=json"
            )
        except Exception as error:
            return [], self._source_status("gdelt", "degraded", str(error))

        raw_articles = payload.get("articles") or []
        additional_languages = tuple(
            item.strip()
            for item in os.getenv("RAGHUB_GDELT_LANGUAGES", "").split(",")
            if item.strip()
        )
        articles, quarantined = self._filter_global_articles(
            raw_articles,
            query=query,
            additional_languages=additional_languages,
        )
        for item in quarantined:
            self.repository.quarantine(
                item,
                str(item.get("_quarantine_reason") or "low_relevance"),
            )
        alerts = []
        for index, article in enumerate(articles[:2], start=1):
            title = str(article.get("title") or "Global event signal")
            domain = str(
                article.get("domain") or article.get("sourcecountry") or "global media"
            )
            tone = float(article.get("tone") or 0.0)
            alerts.append(
                SituationAlert(
                    alert_id=f"gdelt-{index}",
                    category="geopolitical-escalation",
                    severity="high" if tone < -3 else "moderate",
                    title=title,
                    what_happened=(
                        f"GDELT indexed a relevant report from {domain}. "
                        f"It was observed at {article.get('seendate') or 'an unavailable timestamp'}."
                    ),
                    why_it_matters="Fast-changing geopolitical narratives can affect commodities, trade, supply chains, and cross-border risk perception.",
                    who_is_exposed=[
                        "global trade",
                        "commodity-importing industries",
                        "regional risk assets",
                    ],
                    what_could_happen_next="If the reporting cluster spreads geographically or sentiment deteriorates further, market and policy attention can escalate quickly.",
                    evidence_supports=[
                        IntelligenceEvidence(
                            title=title,
                            source="GDELT",
                            summary=f"Media domain {domain} with tone score {tone:.1f}.",
                            url=str(article.get("url") or ""),
                            observed_at=str(article.get("seendate") or ""),
                        )
                    ],
                    invalidation_conditions=[
                        "Narrative intensity cools across the next update window.",
                        "Reported escalation does not propagate into official actions or broader coverage.",
                    ],
                    confidence=0.58,
                    forecast_horizon="days to weeks",
                    tags=["gdelt", "geopolitics"],
                )
            )
        return alerts, self._source_status(
            "gdelt",
            "live",
            (
                f"Retained {len(articles)} relevant English cluster(s); "
                f"quarantined {len(quarantined)} noisy or duplicate item(s)."
            ),
        )

    @staticmethod
    def _filter_global_articles(
        articles: list[dict[str, Any]],
        *,
        query: str,
        additional_languages: tuple[str, ...] = (),
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        allowed_languages = {
            "english",
            *(item.strip().lower() for item in additional_languages if item.strip()),
        }
        domain_terms = {
            "central bank",
            "interest rate",
            "inflation",
            "tariff",
            "sanction",
            "regulation",
            "legislation",
            "government",
            "policy",
            "trade",
            "treasury",
            "fiscal",
            "economic",
            "supply chain",
            "semiconductor",
            "energy",
            "conflict",
        }
        excluded_terms = {
            "nba",
            "nfl",
            "mlb",
            "wnba",
            "soccer",
            "football match",
            "celebrity",
            "box office",
            "entertainment",
        }
        query_terms = {
            token
            for token in "".join(
                character if character.isalnum() else " "
                for character in (query or "").lower()
            ).split()
            if len(token) > 3 and token not in {"global"}
        }
        retained: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        clusters: list[set[str]] = []
        for article in articles:
            item = dict(article)
            title = " ".join(str(item.get("title") or "").split())
            language = str(item.get("language") or "English").lower()
            normalized = {
                token
                for token in "".join(
                    character if character.isalnum() else " "
                    for character in title.lower()
                ).split()
                if len(token) > 2
            }
            reason = ""
            if language not in allowed_languages:
                reason = "language_filtered"
            elif len(title) < 12 or title.replace("-", "").isdigit():
                reason = "malformed_title"
            elif any(term in title.lower() for term in excluded_terms):
                reason = "excluded_sports_or_entertainment"
            elif not (
                any(term in title.lower() for term in domain_terms)
                or (query_terms and normalized & query_terms)
            ):
                reason = "low_domain_relevance"
            elif any(
                len(normalized & prior) / max(1, len(normalized | prior)) >= 0.7
                for prior in clusters
            ):
                reason = "duplicate_cluster"
            if reason:
                item["_quarantine_reason"] = reason
                quarantined.append(item)
                continue
            clusters.append(normalized)
            item["cluster_size"] = 1
            retained.append(item)
        return retained, quarantined

    def _build_bea_status(self) -> dict[str, Any]:
        try:
            payload = self._fetch_json(
                "https://apps.bea.gov/api/data?UserID=GUEST&method=GETDATASETLIST&ResultFormat=json"
            )
        except Exception as error:
            return self._source_status("bea", "degraded", str(error))
        datasets = payload.get("BEAAPI", {}).get("Results", {}).get("Dataset", [])
        return self._source_status(
            "bea",
            "live",
            f"BEA metadata accessible with {len(datasets)} datasets discovered; economic-release connectors can build on this surface.",
        )

    def _build_world_intelligence_graph(
        self,
        alerts: list[SituationAlert],
        government_actions: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        risk_profiles: list[RiskProfile],
    ) -> tuple[list[IntelligenceGraphNode], list[IntelligenceGraphEdge]]:
        nodes: dict[str, IntelligenceGraphNode] = {}
        edges: list[IntelligenceGraphEdge] = []

        def add_node(
            node_id: str,
            label: str,
            node_type: str,
            *,
            score: float = 0.0,
            attributes: dict[str, Any] | None = None,
        ) -> None:
            if node_id not in nodes:
                nodes[node_id] = IntelligenceGraphNode(
                    node_id=node_id,
                    label=label,
                    node_type=node_type,
                    score=score,
                    attributes=attributes or {},
                )

        for alert in alerts:
            add_node(
                alert.alert_id,
                alert.title,
                "alert",
                score=alert.confidence * 100,
                attributes={"category": alert.category, "severity": alert.severity},
            )
            for entity in alert.who_is_exposed:
                entity_id = f"entity:{self._slug(entity)}"
                add_node(
                    entity_id,
                    entity,
                    "exposure",
                    score=alert.confidence * 100,
                    attributes={"source": alert.category},
                )
                edges.append(
                    IntelligenceGraphEdge(
                        source=alert.alert_id,
                        target=entity_id,
                        relationship="exposes",
                        weight=alert.confidence,
                        evidence=alert.what_happened,
                    )
                )
            for evidence in alert.evidence_supports:
                source_id = f"source:{self._slug(evidence.source)}"
                add_node(
                    source_id,
                    evidence.source,
                    "source",
                    score=alert.confidence * 100,
                    attributes={"evidence_id": evidence.evidence_id},
                )
                edges.append(
                    IntelligenceGraphEdge(
                        source=source_id,
                        target=alert.alert_id,
                        relationship="supports",
                        weight=alert.confidence,
                        evidence=evidence.summary,
                    )
                )

        for action in government_actions:
            title = str(action.get("title") or "government-action")
            action_id = f"action:{self._slug(title)}"
            add_node(action_id, title, "government-action", score=72.0)
            sector = str(action.get("sector") or "policy")
            sector_id = f"sector:{self._slug(sector)}"
            add_node(sector_id, sector, "sector", score=72.0)
            edges.append(
                IntelligenceGraphEdge(
                    source=action_id,
                    target=sector_id,
                    relationship="affects",
                    weight=0.72,
                    evidence=str(action.get("timing") or "current"),
                )
            )
            for company in action.get("companies") or []:
                company_id = f"company:{self._slug(company)}"
                add_node(company_id, company, "company", score=68.0)
                edges.append(
                    IntelligenceGraphEdge(
                        source=sector_id,
                        target=company_id,
                        relationship="flows_to",
                        weight=0.68,
                        evidence=str(action.get("market_reaction_window") or ""),
                    )
                )

        for opportunity in opportunities:
            title = str(opportunity.get("title") or "Opportunity")
            opportunity_id = f"opportunity:{self._slug(title)}"
            add_node(
                opportunity_id,
                title,
                "opportunity",
                score=min(
                    100.0, float(opportunity.get("amount") or 0.0) / 1_000_000_000
                ),
            )

        for profile in risk_profiles:
            profile_id = f"risk:{self._slug(profile.subject)}"
            add_node(
                profile_id,
                profile.subject,
                profile.subject_type,
                score=profile.confidence * 100,
            )
            for dimension in profile.dimensions:
                dimension_id = f"dimension:{self._slug(profile.subject)}:{self._slug(dimension.label)}"
                add_node(
                    dimension_id,
                    dimension.label,
                    "risk-dimension",
                    score=dimension.score,
                    attributes={"status": dimension.status},
                )
                edges.append(
                    IntelligenceGraphEdge(
                        source=profile_id,
                        target=dimension_id,
                        relationship="scored_by",
                        weight=dimension.score / 100,
                        evidence=dimension.rationale,
                    )
                )

        return list(nodes.values()), edges

    def _build_investigations(
        self,
        alerts: list[SituationAlert],
        government_actions: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
    ) -> list[Investigation]:
        investigations = []
        if government_actions and opportunities:
            top_action = government_actions[0]
            top_opportunity = opportunities[0]
            investigations.append(
                Investigation(
                    title="Government spending concentration investigation",
                    hypothesis=(
                        f"Sustained public spending through {top_action.get('agency', 'major agencies')} can reinforce demand around {top_opportunity.get('title', 'top recipients')}"
                    ),
                    status="monitoring",
                    confidence=0.69,
                    supporting_entities=[
                        str(top_action.get("agency") or "agency"),
                        str(top_opportunity.get("title") or "recipient"),
                    ],
                    next_steps=[
                        "Monitor follow-on awards and agency concentration.",
                        "Compare recipient concentration with related SEC disclosures.",
                        "Track whether regional employment and capex data confirm the theme.",
                    ],
                    investigation_id="investigation-government-spending",
                    objective="Determine whether spending concentration is producing durable recipient demand.",
                    evidence_collected=[
                        {
                            "summary": str(top_action.get("evidence") or top_action.get("title")),
                            "source": "USAspending",
                        }
                    ],
                    contradicting_evidence=[],
                    open_questions=[
                        "Does award concentration persist after the next refresh?"
                    ],
                    current_conclusion="Evidence supports monitoring, not a completed conclusion.",
                )
            )
        if alerts:
            investigations.append(
                Investigation(
                    title="Autonomous cross-signal review",
                    hypothesis="Macro, fiscal, and filing signals may be converging into a slower but still policy-sensitive late-cycle regime.",
                    status="active",
                    confidence=0.66,
                    supporting_entities=[alert.title for alert in alerts[:3]],
                    next_steps=[
                        "Recheck rate-sensitive sectors after the next macro update.",
                        "Escalate if corporate balance-sheet deterioration broadens across sectors.",
                    ],
                    investigation_id="investigation-cross-signal",
                    objective="Test whether verified macro, fiscal, and filing signals are converging.",
                    evidence_collected=[
                        item.to_dict()
                        for alert in alerts[:3]
                        for item in alert.evidence_supports
                    ],
                    contradicting_evidence=[],
                    open_questions=[
                        "Will the next official macro release confirm or weaken the pattern?"
                    ],
                    current_conclusion="The convergence hypothesis remains active and unconfirmed.",
                    related_forecasts=["fred-cpi-above-3-next-quarter"],
                )
            )
        return investigations

    def _build_decision_journal(
        self,
        alerts: list[SituationAlert],
    ) -> list[DecisionJournalEntry]:
        return [
            DecisionJournalEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                title=alert.title,
                hypothesis=alert.what_could_happen_next,
                probability=alert.confidence,
                horizon=alert.forecast_horizon,
                invalidation_conditions=list(alert.invalidation_conditions),
            )
            for alert in alerts[:6]
        ]

    @staticmethod
    def _build_forecast_tournament() -> list[ForecastModelScore]:
        return [
            ForecastModelScore(
                name="Historical Analogy",
                domain="macro-policy",
                calibration=0.68,
                accuracy=0.64,
                note="Best when policy cycles resemble prior tightening and fiscal stress periods.",
            ),
            ForecastModelScore(
                name="Statistical Baseline",
                domain="macro",
                calibration=0.7,
                accuracy=0.61,
                note="Useful anchor for regime probabilities and growth/price reversion.",
            ),
            ForecastModelScore(
                name="Event Graph Model",
                domain="geopolitical and policy",
                calibration=0.59,
                accuracy=0.57,
                note="Still improving; strongest when multiple government and corporate signals co-occur.",
            ),
            ForecastModelScore(
                name="Ensemble",
                domain="cross-domain",
                calibration=0.74,
                accuracy=0.67,
                note="Combines policy, macro, filings, and spending layers into the current flagship stack.",
            ),
        ]

    def _build_spending_alerts(
        self,
        government_actions: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
    ) -> list[SituationAlert]:
        alerts: list[SituationAlert] = []
        if government_actions:
            action = government_actions[0]
            amount = float(action.get("amount") or 0.0)
            alerts.append(
                SituationAlert(
                    alert_id="usaspending-agency-concentration",
                    category="government-to-market-impact",
                    severity="medium",
                    title=str(action.get("title") or "Agency spending concentration"),
                    what_happened=str(
                        action.get("evidence")
                        or "USAspending surfaced concentrated federal outlays."
                    ),
                    why_it_matters="Agency spending concentration can identify sectors, contractors, and regions likely to benefit or face policy concentration risk.",
                    who_is_exposed=[
                        str(action.get("agency") or "awarding agency"),
                        "government contractors",
                        "regional labor markets",
                    ],
                    what_could_happen_next="If concentration persists, follow-on contracts and capital spending can cluster around the same technology or supplier set.",
                    evidence_supports=[
                        IntelligenceEvidence(
                            title="USAspending awarding-agency category",
                            source="USAspending",
                            summary=f"Tracked agency amount near ${amount / 1_000_000_000:.1f}B.",
                            url="https://www.usaspending.gov/",
                            observed_at="current fiscal year",
                        )
                    ],
                    invalidation_conditions=[
                        "Recipient concentration fades across the next budget window.",
                        "Spending does not translate into follow-on procurement or hiring.",
                    ],
                    confidence=0.73,
                    forecast_horizon="1-3 quarters",
                    tags=["government", "contracts", "agencies"],
                )
            )
        if opportunities:
            top = opportunities[0]
            alerts.append(
                SituationAlert(
                    alert_id="usaspending-opportunity-radar",
                    category="opportunity-radar",
                    severity="moderate",
                    title=f"Federal opportunity radar: {top.get('title', 'top recipient cluster')}",
                    what_happened=f"USAspending shows elevated recipient concentration around {top.get('title', 'a leading award cluster')}.",
                    why_it_matters="Repeated awards and high-dollar concentration can reveal real business-development and investment themes before they become consensus narratives.",
                    who_is_exposed=[
                        str(top.get("title") or "recipient cluster"),
                        "adjacent suppliers",
                        "local economic regions",
                    ],
                    what_could_happen_next="Supplier ecosystems and workforce demand can expand if this award pattern persists into new task orders or adjacent agencies.",
                    evidence_supports=[
                        IntelligenceEvidence(
                            title="USAspending recipient category",
                            source="USAspending",
                            summary=str(
                                top.get("why") or "Recipient concentration detected."
                            ),
                            url="https://www.usaspending.gov/",
                            observed_at="current fiscal year",
                        )
                    ],
                    invalidation_conditions=[
                        "Recipient concentration was driven by a one-off allocation.",
                        "Award modifications do not lead to sustained operational demand.",
                    ],
                    confidence=0.67,
                    forecast_horizon="months",
                    tags=["opportunity", "federal-spending"],
                )
            )
        return alerts

    def _build_congress_alerts(
        self,
        government_actions: list[dict[str, Any]],
    ) -> list[SituationAlert]:
        alerts: list[SituationAlert] = []
        for action in government_actions[:2]:
            if not str(action.get("title") or "").strip():
                continue
            alerts.append(
                SituationAlert(
                    alert_id=f"congress-{self._slug(str(action.get('title') or 'bill'))}",
                    category="government-to-market-impact",
                    severity="moderate",
                    title=str(action.get("title") or "Congressional action"),
                    what_happened="A recent congressional item entered the public legislative feed.",
                    why_it_matters="Bills, hearings, and committee actions can reprice policy-sensitive industries long before final passage.",
                    who_is_exposed=[
                        str(item)
                        for item in action.get("companies")
                        or ["policy-sensitive sectors"]
                    ],
                    what_could_happen_next="Sector winners, losers, contractors, and regional beneficiaries can become clearer as committees, summaries, and vote timing evolve.",
                    evidence_supports=[
                        IntelligenceEvidence(
                            title="Congress.gov feed",
                            source="Congress.gov",
                            summary=str(
                                action.get("timing") or "Legislative timing pending."
                            ),
                            observed_at=str(action.get("timing") or ""),
                        )
                    ],
                    invalidation_conditions=[
                        "The bill stalls before committee or floor progress.",
                        "Final language removes the economically sensitive provisions.",
                    ],
                    confidence=0.61,
                    forecast_horizon="weeks to quarters",
                    tags=["congress", "policy"],
                )
            )
        return alerts

    def _macro_alert(self, regime: MacroRegime) -> SituationAlert:
        indicators = regime.indicators
        return SituationAlert(
            alert_id="macro-regime-detector",
            category="macro-regime",
            severity="medium",
            title=f"Macro regime detector: {regime.regime}",
            what_happened=(
                f"Latest FRED signals show CPI running near {indicators.get('cpi_yoy', 0):.1f}% year over year, "
                f"the unemployment rate up {indicators.get('unemployment_change_yoy', 0):.1f} points year over year, "
                f"and fed funds near {indicators.get('fed_funds', 0):.1f}%."
            ),
            why_it_matters="Macro regime shifts shape sector leadership, discount rates, refinancing conditions, policy sensitivity, and default risk.",
            who_is_exposed=[
                "rate-sensitive equities",
                "credit markets",
                "housing",
                "cyclical industries",
            ],
            what_could_happen_next=regime.summary,
            evidence_supports=[
                IntelligenceEvidence(
                    title="FRED macro series",
                    source="FRED",
                    summary="CPIAUCSL, UNRATE, and FEDFUNDS refreshed successfully.",
                    url="https://fred.stlouisfed.org/",
                )
            ],
            invalidation_conditions=[
                "Inflation and labor trends diverge materially from the current pattern.",
                "Policy rates change regime faster than the labor and price data imply.",
            ],
            confidence=regime.confidence,
            forecast_horizon="1-4 quarters",
            tags=["macro", "fred", regime.regime.lower()],
        )

    def _fetch_fred_series(self, series_id: str) -> list[tuple[str, float]]:
        if not self.fred_api_key:
            raise RuntimeError("FRED_API_KEY is not configured.")
        payload = self._fetch_provider_json(
            "https://api.stlouisfed.org/fred/series/observations",
            provider="FRED",
            params={
                "series_id": series_id,
                "api_key": self.fred_api_key,
                "file_type": "json",
                "sort_order": "asc",
            },
        )
        values: list[tuple[str, float]] = []
        for row in payload.get("observations") or []:
            if not isinstance(row, dict):
                continue
            raw = row.get("value")
            if raw in (None, "", "."):
                continue
            values.append((str(row.get("date") or ""), float(raw)))
        return values

    def _fetch_provider_json(
        self,
        url: str,
        *,
        provider: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Fetch authenticated provider data without leaking credentials in errors."""
        try:
            response = self.session.get(
                url,
                params=params,
                headers={
                    "User-Agent": "RAGHub/1.0 research@local.test",
                    "Accept": "application/json",
                },
                timeout=20,
            )
            response.raise_for_status()
            parsed = response.json()
        except (requests.RequestException, ValueError, TypeError) as error:
            raise RuntimeError(
                f"{provider} request failed ({type(error).__name__})."
            ) from error
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{provider} returned an unexpected response.")
        return parsed

    def _fetch_world_bank_indicator(self, country_code: str, indicator: str) -> float:
        payload = self._fetch_json(
            "https://api.worldbank.org/v2/country/"
            f"{country_code}/indicator/{indicator}?format=json&per_page=6"
        )
        if not isinstance(payload, list) or len(payload) < 2:
            raise ValueError("Unexpected World Bank payload")
        for row in payload[1]:
            value = row.get("value") if isinstance(row, dict) else None
            if value is not None:
                return float(value)
        return 0.0

    def _latest_company_fact(
        self,
        payload: dict[str, Any],
        taxonomy: str,
        candidates: list[str],
    ) -> float:
        facts = payload.get("facts", {}).get(taxonomy, {})
        for candidate in candidates:
            unit_map = facts.get(candidate, {}).get("units", {})
            usd_rows = unit_map.get("USD") or unit_map.get("USD/shares") or []
            cleaned = [row for row in usd_rows if row.get("val") is not None]
            cleaned.sort(
                key=lambda row: str(row.get("end") or row.get("fy") or ""), reverse=True
            )
            if cleaned:
                return float(cleaned[0].get("val") or 0.0)
        return 0.0

    def _fetch_json(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "User-Agent": "RAGHub/1.0 research@local.test",
            "Accept": "application/json",
        }
        if method.upper() == "POST":
            response = self.session.post(
                url,
                headers={**headers, "Content-Type": "application/json"},
                json=payload or {},
                timeout=20,
            )
        else:
            response = self.session.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def _fetch_text(self, url: str) -> str:
        response = self.session.get(
            url,
            headers={"User-Agent": "RAGHub/1.0 research@local.test"},
            timeout=20,
        )
        response.raise_for_status()
        return response.text

    @staticmethod
    def _source_status(source: str, status: str, detail: str) -> dict[str, Any]:
        return {
            "source": source,
            "status": status,
            "detail": detail,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _normalize_probabilities(values: list[float]) -> tuple[float, float, float]:
        clipped = [max(0.01, value) for value in values]
        total = sum(clipped)
        if total <= 0:
            return (0.34, 0.33, 0.33)
        normalized = [value / total for value in clipped]
        return normalized[0], normalized[1], normalized[2]

    @staticmethod
    def _slug(value: str) -> str:
        return "-".join(
            part
            for part in "".join(
                ch.lower() if ch.isalnum() else " " for ch in value
            ).split()
            if part
        )
