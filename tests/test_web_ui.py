from io import BytesIO
import unittest

from web_ui import FeatureUIHandler


class FakeTrendingPlayer:
    def __init__(self, **attributes):
        self.__dict__.update(attributes)


class WebUITests(unittest.TestCase):
    def test_render_form_uses_live_provider_results(self):
        handler = object.__new__(FeatureUIHandler)
        handler.trending_player_provider = lambda: [
            FakeTrendingPlayer(
                player_name="Sky Ace",
                team_name="Live Stars",
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

        self.assertIn("Live Cached Data", html)
        self.assertNotIn("Demo Data", html)
        self.assertEqual(html.count('class="player-card"'), 2)
        self.assertIn("Sky Ace", html)
        self.assertIn("Court Calm", html)
        self.assertIn("rising", html)
        self.assertIn("stable", html)
        self.assertIn("Refresh NBA Data", html)
        self.assertIn("Manual Player Feature Form", html)
        self.assertIn('action="/refresh"', html)
        self.assertIn('name="performance_history"', html)

    def test_render_form_falls_back_to_demo_cards(self):
        handler = object.__new__(FeatureUIHandler)
        handler.trending_player_provider = lambda: []

        html = FeatureUIHandler.render_form(handler)

        self.assertIn("Demo Data", html)
        self.assertNotIn("Live Cached Data", html)
        self.assertEqual(html.count('class="player-card"'), 3)
        self.assertIn("Nova Carter", html)
        self.assertIn("Milo Grant", html)
        self.assertIn("Zuri Lane", html)
        self.assertIn("Refresh NBA Data", html)
        self.assertIn("Manual Player Feature Form", html)
        self.assertIn('name="performance_history"', html)

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


if __name__ == "__main__":
    unittest.main()
