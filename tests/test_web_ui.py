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
        handler.trending_player_provider = lambda: [
            FakeTrendingPlayer(
                player_name="Sky Ace",
                team_name="Live Stars",
                league="WNBA",
                competition="Regular Season",
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

        self.assertIn("Historical Basketball Cache", html)
        self.assertIn("League: WNBA", html)
        self.assertIn("Competition: Regular Season", html)
        self.assertIn("Last refresh: 2026-07-23 09:15 UTC", html)
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
        self.assertNotIn("Historical Basketball Cache", html)
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

        self.assertIn("Historical Basketball Cache", html)
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


if __name__ == "__main__":
    unittest.main()
