import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import requests

from app import create_app
from intelligence.models import (
    IntelligenceEvidence,
    MacroRegime,
    RiskDimension,
    RiskProfile,
    SituationAlert,
)
from intelligence.service import SituationRoomService
from intelligence.repository import SituationRoomRepository


class FakeSituationRoomService(SituationRoomService):
    def __init__(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        super().__init__(
            ttl_seconds=1,
            repository=SituationRoomRepository(
                Path(self._temporary_directory.name) / "situation-room.db"
            ),
        )

    def _build_macro_regime(self):
        return (
            MacroRegime(
                regime="Inflationary growth",
                confidence=0.8,
                summary="Prices remain elevated while activity is still resilient.",
                indicators={
                    "cpi_yoy": 3.5,
                    "unemployment_change_yoy": 0.2,
                    "fed_funds": 4.5,
                },
            ),
            self._source_status("fred", "live", "ok"),
        )

    def _build_fiscal_monitor(self):
        return (
            SituationAlert(
                alert_id="fiscal-1",
                category="fiscal-sustainability",
                severity="high",
                title="Debt burden rising",
                what_happened="Debt increased materially.",
                why_it_matters="Refinancing pressure can rise.",
                who_is_exposed=["Treasury market", "long-duration assets"],
                what_could_happen_next="Issuance sensitivity may increase.",
                evidence_supports=[
                    IntelligenceEvidence(
                        title="Treasury debt series",
                        source="treasury",
                        summary="Debt series updated.",
                        url="https://fiscaldata.treasury.gov/",
                        observed_at="2026-01-15",
                    )
                ],
                invalidation_conditions=["Debt growth slows."],
                confidence=0.8,
                forecast_horizon="2 quarters",
            ),
            self._source_status("treasury", "live", "ok"),
        )

    def _build_spending_radar(self):
        return (
            [
                {
                    "title": "Defense spending concentration",
                    "agency": "Department of Defense",
                    "sector": "defense",
                    "amount": 123000000000.0,
                    "timing": "2025",
                    "market_reaction_window": "budget cycle",
                    "companies": ["Lockheed Martin"],
                    "evidence": "Large DoD obligations",
                }
            ],
            [
                {
                    "title": "Lockheed Martin",
                    "amount": 54000000000.0,
                    "why": "Large recipient concentration detected.",
                    "next": "Monitor follow-on awards.",
                }
            ],
            self._source_status("usaspending", "live", "ok"),
        )

    def _build_corporate_engine(self):
        return (
            [
                SituationAlert(
                    alert_id="sec-1",
                    category="corporate-early-warning",
                    severity="medium",
                    title="Apple filing monitor",
                    what_happened="Recent 10-Q observed.",
                    why_it_matters="Balance-sheet changes can matter.",
                    who_is_exposed=["Apple", "consumer electronics"],
                    what_could_happen_next="Liquidity may remain strong.",
                    evidence_supports=[
                        IntelligenceEvidence(
                            title="SEC filing",
                            source="sec",
                            summary="10-Q update",
                            url="https://www.sec.gov/edgar",
                            observed_at="2026-01-15",
                        )
                    ],
                    invalidation_conditions=["Next filing weakens the thesis."],
                    confidence=0.7,
                    forecast_horizon="1 quarter",
                )
            ],
            [
                RiskProfile(
                    subject="Apple",
                    subject_type="company",
                    summary="Large-cap balance sheet profile.",
                    confidence=0.75,
                    dimensions=[
                        RiskDimension(
                            label="Liquidity",
                            score=82.0,
                            status="High",
                            rationale="Cash coverage is strong.",
                        )
                    ],
                )
            ],
            [
                {
                    "title": "Apple policy exposure",
                    "sector": "electronics",
                    "companies": ["Apple"],
                    "timing": "next filing cycle",
                    "historical_analogues": ["prior supply-chain shocks"],
                    "market_reaction_window": "filing window",
                }
            ],
            self._source_status("sec", "live", "ok"),
        )

    def _build_country_risk_engine(self):
        return (
            [
                RiskProfile(
                    subject="United States",
                    subject_type="country",
                    summary="Country risk profile.",
                    confidence=0.65,
                    dimensions=[
                        RiskDimension(
                            label="Economic Stability",
                            score=76.0,
                            status="High",
                            rationale="Growth remains positive.",
                        )
                    ],
                )
            ],
            self._source_status("world-bank", "live", "ok"),
        )

    def _build_congress_actions(self):
        return (
            [],
            self._source_status("congress", "unconfigured", "missing key"),
        )

    def _build_global_event_monitor(self, query):
        return (
            [],
            self._source_status("gdelt", "degraded", "rate limited"),
        )

    def _build_bea_status(self):
        return self._source_status("bea", "live", "metadata only")


class SituationRoomServiceTests(unittest.TestCase):
    def test_fred_series_uses_authenticated_json_api(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "observations": [
                        {"date": "2026-01-01", "value": "321.5"},
                        {"date": "2026-02-01", "value": "."},
                    ]
                }

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        service = SituationRoomService(session=session, fred_api_key="fred-secret")

        values = service._fetch_fred_series("CPIAUCSL")

        self.assertEqual(values, [("2026-01-01", 321.5)])
        url, kwargs = session.calls[0]
        self.assertEqual(
            url, "https://api.stlouisfed.org/fred/series/observations"
        )
        self.assertNotIn("fred-secret", url)
        self.assertEqual(kwargs["params"]["api_key"], "fred-secret")
        self.assertEqual(kwargs["params"]["series_id"], "CPIAUCSL")
        self.assertEqual(kwargs["params"]["file_type"], "json")

    def test_api_data_gov_key_configures_congress_without_legacy_name(self):
        with patch.dict(
            "os.environ",
            {"API_DATA_GOV_KEY": "data-gov-secret"},
            clear=True,
        ):
            service = SituationRoomService()

        self.assertEqual(service.congress_api_key, "data-gov-secret")

    def test_congress_request_does_not_embed_key_in_endpoint(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"bills": []}

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        service = SituationRoomService(
            session=session, api_data_gov_key="data-gov-secret"
        )

        actions, status = service._build_congress_actions()

        self.assertEqual(actions, [])
        self.assertEqual(status["status"], "live")
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.congress.gov/v3/bill")
        self.assertNotIn("data-gov-secret", url)
        self.assertEqual(kwargs["params"]["api_key"], "data-gov-secret")
        self.assertEqual(kwargs["params"]["format"], "json")

    def test_authenticated_provider_errors_do_not_expose_credentials(self):
        class Session:
            def get(self, url, **kwargs):
                secret = kwargs["params"]["api_key"]
                raise requests.HTTPError(f"failed request api_key={secret}")

        service = SituationRoomService(
            session=Session(),
            fred_api_key="fred-secret",
            api_data_gov_key="data-gov-secret",
        )

        macro, fred_status = service._build_macro_regime()
        _actions, congress_status = service._build_congress_actions()

        self.assertIsNone(macro)
        self.assertNotIn("fred-secret", fred_status["detail"])
        self.assertNotIn("data-gov-secret", congress_status["detail"])

    def test_snapshot_builds_explainable_alerts_and_graph(self):
        service = FakeSituationRoomService()

        snapshot = service.get_snapshot()

        self.assertIn("alerts", snapshot)
        self.assertIn("graph_nodes", snapshot)
        self.assertIn("graph_edges", snapshot)
        self.assertGreaterEqual(len(snapshot["alerts"]), 3)
        self.assertGreater(snapshot["graph_summary"]["node_count"], 0)
        self.assertGreater(snapshot["graph_summary"]["edge_count"], 0)
        self.assertEqual(snapshot["macro_regime"]["regime"], "Inflationary growth")
        self.assertTrue(
            any(item["source"] == "congress" for item in snapshot["source_status"])
        )
        conclusions = snapshot["user_impact_conclusions"]
        self.assertEqual(len(conclusions), len(snapshot["alerts"]))
        fiscal_impact = next(
            item for item in conclusions if item["alert_id"] == "fiscal-1"
        )
        self.assertEqual(fiscal_impact["impact_level"], "high")
        self.assertIn("Treasury market", fiscal_impact["affected_areas"])
        self.assertIn("Refinancing pressure", fiscal_impact["direct_effect"])
        self.assertIn("review", fiscal_impact["recommended_action"].lower())
        self.assertEqual(fiscal_impact["confidence"], 0.8)
        self.assertEqual(fiscal_impact["forecast_horizon"], "2 quarters")
        self.assertEqual(
            fiscal_impact["invalidation_conditions"], ["Debt growth slows."]
        )
        summary = snapshot["plain_language_summary"]
        self.assertIn("headline", summary)
        self.assertIn("in_plain_english", summary)
        self.assertIn("how_it_may_affect_you", summary)
        self.assertGreaterEqual(len(summary["in_plain_english"]), 2)
        self.assertGreaterEqual(len(snapshot["suggestion_prompts"]), 3)
        self.assertTrue(
            all(item["prompt"] for item in snapshot["suggestion_prompts"])
        )
        integrity = snapshot["data_integrity"]
        self.assertEqual(integrity["alert_count"], len(snapshot["alerts"]))
        self.assertEqual(
            integrity["evidence_unit_count"],
            sum(len(alert["evidence_supports"]) for alert in snapshot["alerts"]),
        )
        self.assertTrue(all(item["checked_at"] for item in snapshot["source_status"]))
        self.assertTrue(
            all(
                evidence["url"] and evidence["source"] and evidence["observed_at"]
                for alert in snapshot["alerts"]
                for evidence in alert["evidence_supports"]
            )
        )
        self.assertTrue(all(item["data_classification"] for item in snapshot["forecast_tournament"]))

    def test_snapshot_has_canonical_situations_and_shared_ids(self):
        snapshot = FakeSituationRoomService().get_snapshot()

        self.assertTrue(snapshot["situations"])
        situation_ids = {item["id"] for item in snapshot["situations"]}
        self.assertIn(snapshot["primary_situation_id"], situation_ids)
        self.assertTrue(
            all(alert["situation_id"] in situation_ids for alert in snapshot["alerts"])
        )
        for collection in (
            snapshot["government_actions"],
            snapshot["opportunities"],
            snapshot["investigations"],
            snapshot["decision_journal"],
        ):
            self.assertTrue(
                all(item["situation_id"] in situation_ids for item in collection)
            )
        primary = next(
            item
            for item in snapshot["situations"]
            if item["id"] == snapshot["primary_situation_id"]
        )
        self.assertTrue(primary["summary"])
        self.assertTrue(primary["evidence"])
        self.assertTrue(primary["forecasts"])
        self.assertTrue(primary["recommended_actions"])
        self.assertEqual(primary["current_lifecycle_stage"], "monitor")
        self.assertTrue(primary["intelligence_objective"])
        self.assertTrue(primary["new_observations"])
        self.assertTrue(primary["evidence_collected"])
        self.assertIn("evidence_gaps", primary)
        self.assertTrue(primary["active_hypotheses"])
        self.assertTrue(primary["monitoring_rules"])
        self.assertIn("final_outcome", primary)
        self.assertIn("lessons_learned", primary)
        self.assertTrue(all(item["event_type"] for item in primary["history"]))

    def test_graph_endpoint_structure_is_separate_from_full_snapshot(self):
        service = FakeSituationRoomService()

        graph = service.get_graph()

        self.assertIn("nodes", graph)
        self.assertEqual(graph["summary"]["node_count"], len(graph["nodes"]))
        self.assertEqual(graph["summary"]["edge_count"], len(graph["edges"]))
        self.assertIn("edges", graph)
        self.assertIn("summary", graph)
        self.assertGreater(len(graph["nodes"]), 0)


class SituationRoomFlaskRoutesTests(unittest.TestCase):
    def test_situation_room_routes_render_and_return_json(self):
        app = create_app(situation_room_service=FakeSituationRoomService())
        client = app.test_client()

        html_response = client.get("/situation-room")
        api_response = client.get("/api/situation-room")
        graph_response = client.get("/api/situation-room/graph")
        situation_response = client.get("/api/situation-room/situations/fiscal-1")

        self.assertEqual(html_response.status_code, 200)
        self.assertIn(b"RAGHub Situation Room", html_response.get_data())
        self.assertIn(b"Decision workspace", html_response.get_data())
        self.assertIn(b"Intelligence Brief", html_response.get_data())
        self.assertIn(b"Active Forecasts", html_response.get_data())
        self.assertIn(b"Evidence Explorer", html_response.get_data())
        self.assertIn(b"forecastProbabilityChart", html_response.get_data())
        self.assertEqual(api_response.status_code, 200)
        self.assertIn("alerts", api_response.get_json())
        self.assertIn("user_impact_conclusions", api_response.get_json())
        self.assertIn("situations", api_response.get_json())
        self.assertEqual(graph_response.status_code, 200)
        self.assertIn("nodes", graph_response.get_json())
        self.assertEqual(situation_response.status_code, 200)
        self.assertEqual(
            [item["stage"] for item in situation_response.get_json()["brief_stages"]],
            ["Observe", "Retrieve", "Analyze", "Forecast", "Recommend", "Monitor", "Learn"],
        )

    def test_graph_api_returns_explicit_degraded_payload_instead_of_502(self):
        class BrokenGraphService(FakeSituationRoomService):
            def get_graph(self, **_kwargs):
                raise RuntimeError("graph build failed")

        app = create_app(situation_room_service=BrokenGraphService())
        response = app.test_client().get("/api/situation-room/graph")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["nodes"], [])
        self.assertEqual(payload["edges"], [])
        self.assertEqual(payload["summary"]["node_count"], 0)

    def test_situation_room_legacy_page_renders(self):
        app = create_app(situation_room_service=FakeSituationRoomService())
        client = app.test_client()
        response = client.get("/situation-room/legacy")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Global Intelligence Situation Room", response.data)
        self.assertIn(b"What These Events Mean for You", response.data)

    def test_situation_room_autonomy_api_controls(self):
        app = create_app(situation_room_service=FakeSituationRoomService())
        client = app.test_client()

        status_response = client.get("/api/situation-room/autonomy")
        self.assertEqual(status_response.status_code, 200)
        self.assertIn("enabled", status_response.get_json())

        start_response = client.post(
            "/api/situation-room/autonomy",
            json={"action": "start", "interval_seconds": 60, "focus": "macro"},
        )
        self.assertEqual(start_response.status_code, 200)
        self.assertTrue(start_response.get_json()["enabled"])

        run_response = client.post(
            "/api/situation-room/autonomy",
            json={"action": "run", "focus": "macro"},
        )
        self.assertEqual(run_response.status_code, 200)
        run_payload = run_response.get_json()
        self.assertEqual(run_payload.get("status"), "ok")
        self.assertIn("actions", run_payload)

    def test_situation_room_prediction_api(self):
        app = create_app(situation_room_service=FakeSituationRoomService())
        client = app.test_client()

        response = client.post(
            "/api/situation-room/predict",
            json={
                "focus": "macro",
                "scenario": "Energy sanctions tighten",
                "horizon_days": 90,
                "confidence_mode": "balanced",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("summary", payload)
        self.assertIn("probabilities", payload)
        self.assertIn("forecast_path", payload)
        self.assertEqual(payload["data_classification"], "simulated_scenario")
        self.assertEqual(payload.get("horizon_days"), 90)
        self.assertGreaterEqual(len(payload["forecast_path"]), 8)
        for point in payload["forecast_path"]:
            total = point["base_case"] + point["upside"] + point["downside"]
            self.assertAlmostEqual(total, 1.0, places=2)
            self.assertLessEqual(point["base_low"], point["base_case"])
            self.assertGreaterEqual(point["base_high"], point["base_case"])


if __name__ == "__main__":
    unittest.main()
