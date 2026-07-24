from io import BytesIO
import unittest

from sports.intelligence.models.trending_player import TrendingPlayer
from web_ui import FeatureUIHandler


class FakeTrendingPlayer:
    def __init__(self, **attributes):
        self.__dict__.update(attributes)


class WebUITests(unittest.TestCase):
    def test_render_form_uses_historical_cache_results(self):
        handler = object.__new__(FeatureUIHandler)
        handler.historical_load_status_provider = lambda: (
            "Loaded live basketball cache from Official NBA CDN with 246 games and 30 players."
        )
        handler.historical_load_status_details_provider = lambda: {
            "success": True,
            "source_label": "Official NBA CDN",
            "games_loaded": 246,
            "players_loaded": 30,
            "records_loaded": 246,
        }
        handler.trending_player_provider = lambda: [
            FakeTrendingPlayer(
                player_name="Sky Ace",
                team_name="Live Stars",
                league="WNBA",
                competition="Regular Season",
                games_loaded=246,
                players_loaded=30,
                trend_label="rising",
                recent_five_average=21.4,
                current_season_average=19.2,
                previous_season_average=16.1,
                weighted_two_season_average=17.8,
                explanation="He keeps winning the late-game battles.",
                last_refreshed_at="2026-07-23 09:15 UTC",
            ),
            FakeTrendingPlayer(
                player_name="Court Calm",
                team_name="Live Stars",
                league="WNBA",
                competition="Regular Season",
                trend_label="stable",
                recent_five_average=13.0,
                current_season_average=12.8,
                previous_season_average=12.4,
                weighted_two_season_average=12.6,
                explanation="His production barely moves, which SIP likes.",
                last_refreshed_at="2026-07-23 09:15 UTC",
            ),
        ]

        html = FeatureUIHandler.render_form(handler)

        self.assertIn("Official NBA CDN", html)
        self.assertNotIn("Historical Basketball Cache", html)
        self.assertIn("League: WNBA", html)
        self.assertIn("Competition: Regular Season", html)
        self.assertIn("Last refresh: 2026-07-23 09:15 UTC", html)
        self.assertIn("Games loaded: 246", html)
        self.assertIn("Players loaded: 30", html)
        self.assertIn("Load status:", html)
        self.assertIn(
            "Loaded live basketball cache from Official NBA CDN with 246 games and 30 players.",
            html,
        )
        self.assertEqual(html.count('class="player-card"'), 2)
        self.assertIn("Sky Ace", html)
        self.assertIn("Court Calm", html)
        self.assertIn("rising", html)
        self.assertIn("stable", html)
        self.assertIn("Refresh NBA Data", html)
        self.assertIn("Manual Sandbox — user-entered values", html)
        self.assertIn('action="/refresh"', html)
        self.assertIn('name="performance_history"', html)

    def test_render_form_empty_cache_shows_load_history_state(self):
        handler = object.__new__(FeatureUIHandler)
        handler.historical_load_status_provider = lambda: (
            "Could not load historical basketball data."
        )
        handler.historical_load_status_details_provider = lambda: {
            "success": False,
            "source_label": None,
            "games_loaded": 0,
            "players_loaded": 0,
            "records_loaded": 0,
        }
        handler.trending_player_provider = lambda: []

        html = FeatureUIHandler.render_form(handler)

        self.assertIn("Basketball data not loaded", html)
        self.assertIn(
            "Historical NBA/WNBA data must be loaded before trending players can be shown.",
            html,
        )
        self.assertIn("Load Historical Data", html)
        self.assertIn('action="/load-history"', html)
        self.assertEqual(html.count('class="player-card"'), 0)
        self.assertNotIn("Official NBA CDN", html)
        self.assertNotIn("ESPN public web data", html)
        self.assertIn("Load status:", html)
        self.assertIn("Could not load historical basketball data.", html)
        self.assertNotIn("Games loaded:", html)
        self.assertNotIn("Players loaded:", html)
        self.assertNotIn("Nova Carter", html)
        self.assertNotIn("Milo Grant", html)
        self.assertNotIn("Zuri Lane", html)
        self.assertIn("Manual Sandbox — user-entered values", html)
        self.assertIn('name="performance_history"', html)

    def test_load_history_post_redirects_back_to_root(self):
        handler = object.__new__(FeatureUIHandler)
        events = {"load_history_calls": 0, "status": None, "headers": []}

        handler.path = "/load-history"
        handler.headers = {}
        handler.rfile = BytesIO(b"")
        handler.wfile = BytesIO()
        handler.load_history_callback = lambda: events.__setitem__(
            "load_history_calls",
            events["load_history_calls"] + 1,
        )
        handler.send_response = lambda status_code: events.__setitem__(
            "status",
            status_code,
        )
        handler.send_header = lambda name, value: events["headers"].append(
            (name, value)
        )
        handler.end_headers = lambda: None

        FeatureUIHandler.do_POST(handler)

        self.assertEqual(events["load_history_calls"], 1)
        self.assertEqual(events["status"], 303)
        self.assertIn(("Location", "/"), events["headers"])

    def test_refresh_post_redirects_back_to_root(self):
        handler = object.__new__(FeatureUIHandler)
        events = {"refresh_calls": 0, "status": None, "headers": []}

        handler.path = "/refresh"
        handler.headers = {}
        handler.rfile = BytesIO(b"")
        handler.wfile = BytesIO()
        handler.refresh_callback = lambda: events.__setitem__(
            "refresh_calls",
            events["refresh_calls"] + 1,
        )
        handler.send_response = lambda status_code: events.__setitem__(
            "status",
            status_code,
        )
        handler.send_header = lambda name, value: events["headers"].append(
            (name, value)
        )
        handler.end_headers = lambda: None

        FeatureUIHandler.do_POST(handler)

        self.assertEqual(events["refresh_calls"], 1)
        self.assertEqual(events["status"], 303)
        self.assertIn(("Location", "/"), events["headers"])

    def test_render_form_supports_real_trending_player_instance(self):
        handler = object.__new__(FeatureUIHandler)
        handler.historical_load_status_details_provider = lambda: {
            "success": True,
            "source_label": "ESPN public web data",
            "games_loaded": 8,
            "players_loaded": 12,
            "records_loaded": 96,
        }
        handler.trending_player_provider = lambda: [
            TrendingPlayer(
                player_id="2544",
                player_name="LeBron James",
                latest_team="Los Angeles Lakers",
                recent_five_ppg=28.2,
                current_season_ppg=25.4,
                previous_season_ppg=24.1,
                weighted_two_season_ppg=24.8,
                trend_score=2.8,
                badge="rising",
                explanation="Recent five games are tracking above his season baseline.",
            )
        ]

        html = FeatureUIHandler.render_form(handler)

        self.assertIn("ESPN public web data", html)
        self.assertIn("Games loaded: 8", html)
        self.assertIn("Players loaded: 12", html)
        self.assertIn("LeBron James", html)
        self.assertIn("Los Angeles Lakers", html)
        self.assertIn("28.2", html)
        self.assertIn("25.4", html)
        self.assertIn("24.1", html)
        self.assertIn("24.8", html)
        self.assertIn("rising", html)
        self.assertIn("Recent 5-game average", html)
        self.assertIn("Current-season average", html)
        self.assertIn("Previous-season average", html)
        self.assertIn("Weighted two-season average", html)
        self.assertIn(
            "Recent five games are tracking above his season baseline.",
            html,
        )

    def test_render_form_shows_learning_intelligence_when_alerts_evaluated(self):
        handler = object.__new__(FeatureUIHandler)
        handler.historical_load_status_details_provider = lambda: {
            "success": True,
            "source_label": "Official NBA CDN",
            "games_loaded": 12,
            "players_loaded": 8,
            "records_loaded": 96,
        }
        handler.trending_player_provider = lambda: [
            FakeTrendingPlayer(
                player_id="101",
                player_name="A Real Player",
                team_name="Live Team",
                league="NBA",
                competition="Regular Season",
                trend_label="rising",
                recent_five_average=24.0,
                current_season_average=20.0,
                previous_season_average=18.0,
                weighted_two_season_average=19.0,
                explanation="Real production is climbing.",
                last_refreshed_at="2026-07-23 09:15 UTC",
            )
        ]
        handler.learning_summary_provider = lambda: {
            "evaluated_alerts": 6,
            "accuracy": 0.67,
            "avg_confidence": 0.72,
            "avg_confidence_error": 0.11,
            "calibration_error": 0.093,
            "calibration_buckets": [],
            "hypothesis_updates": [
                {
                    "recommendation": "Increase confidence and reuse this hypothesis in similar alerts."
                }
            ],
            "reusable_knowledge": [
                "Signal 'scoring_spike' accuracy 67% with confidence error 0.11."
            ],
        }

        html = FeatureUIHandler.render_form(handler)

        self.assertIn("Learning intelligence:", html)
        self.assertIn("Evaluated alerts", html)
        self.assertIn("Outcome accuracy", html)
        self.assertIn("Calibration error: 0.093", html)
        self.assertIn("Top recommendation:", html)
        self.assertIn("Top reusable insight:", html)

    def test_render_form_hides_learning_intelligence_when_no_alerts(self):
        handler = object.__new__(FeatureUIHandler)
        handler.historical_load_status_details_provider = lambda: {
            "success": True,
            "source_label": "ESPN public web data",
            "games_loaded": 4,
            "players_loaded": 5,
            "records_loaded": 20,
        }
        handler.trending_player_provider = lambda: [
            FakeTrendingPlayer(
                player_id="201",
                player_name="Another Real Player",
                team_name="Live Team",
                league="WNBA",
                competition="Regular Season",
                trend_label="stable",
                recent_five_average=15.0,
                current_season_average=15.0,
                previous_season_average=14.8,
                weighted_two_season_average=14.9,
                explanation="Steady outputs across recent games.",
                last_refreshed_at="2026-07-23 09:15 UTC",
            )
        ]
        handler.learning_summary_provider = lambda: {
            "evaluated_alerts": 0,
            "accuracy": 0.0,
            "avg_confidence": 0.0,
            "avg_confidence_error": 0.0,
            "calibration_error": 0.0,
            "calibration_buckets": [],
            "hypothesis_updates": [],
            "reusable_knowledge": [],
        }

        html = FeatureUIHandler.render_form(handler)

        self.assertNotIn("Learning intelligence:", html)

    def test_render_form_handles_provider_exception_without_crashing(self):
        handler = object.__new__(FeatureUIHandler)
        handler.historical_load_status_provider = lambda: None
        handler.historical_load_status_details_provider = lambda: None

        def broken_provider(*args, **kwargs):
            raise RuntimeError("database is locked")

        handler.trending_player_provider = broken_provider

        html = FeatureUIHandler.render_form(handler)

        self.assertIn("Load status:", html)
        self.assertIn("Could not load historical basketball data.", html)
        self.assertIn("database is locked", html)
        self.assertIn("Load Historical Data", html)

    def test_render_form_uses_rebuild_label_for_legacy_schema_error(self):
        handler = object.__new__(FeatureUIHandler)
        handler.historical_load_status_provider = lambda: (
            "Unable to read historical basketball cache. Details: no such column: league"
        )
        handler.historical_load_status_details_provider = lambda: None
        handler.trending_player_provider = lambda: []

        html = FeatureUIHandler.render_form(handler)

        self.assertIn("Load status:", html)
        self.assertIn("no such column: league", html)
        self.assertIn("Rebuild Historical Data Cache", html)


if __name__ == "__main__":
    unittest.main()
