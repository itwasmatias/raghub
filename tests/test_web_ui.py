import unittest

from web_ui import FeatureUIHandler


class WebUITests(unittest.TestCase):
    def test_render_form_shows_sip_demo_cards_and_manual_form(self):
        handler = object.__new__(FeatureUIHandler)
        html = FeatureUIHandler.render_form(handler)

        self.assertIn("Demo Data", html)
        self.assertIn("SIP Trending Players", html)
        self.assertEqual(html.count('class="player-card"'), 3)
        self.assertIn("Nova Carter", html)
        self.assertIn("Milo Grant", html)
        self.assertIn("Zuri Lane", html)
        self.assertIn("rising", html)
        self.assertIn("stable", html)
        self.assertIn("declining", html)
        self.assertIn("Recent 5-game average", html)
        self.assertIn("Current-season average", html)
        self.assertIn("Previous-season average", html)
        self.assertIn("Weighted two-season average", html)
        self.assertIn("Why SIP noticed him", html)
        self.assertIn("Last refreshed:", html)
        self.assertIn("Manual Player Feature Form", html)
        self.assertIn('name="performance_history"', html)
        self.assertLess(
            html.index("Demo Data"), html.index("Manual Player Feature Form")
        )


if __name__ == "__main__":
    unittest.main()
