import unittest

import app as app_module


class FlaskAppTests(unittest.TestCase):
    def test_betting_route_is_available(self):
        class FakeRuntime:
            get_trending_players = staticmethod(lambda: [])
            load_history = staticmethod(lambda: None)
            get_load_status_message = staticmethod(lambda: None)
            get_load_status_details = staticmethod(lambda: None)
            refresh = staticmethod(lambda: None)

            def get_betting_assessments(self):
                return []

            def get_betting_board(self):
                return {
                    "status": "unconfigured",
                    "assessments": [],
                    "message": "Live odds are not configured.",
                }

            def get_betting_replay_board(self):
                return {
                    "data_classification": "replay",
                    "status": "ready",
                    "assessments": [],
                    "message": "Historical replay payload reached the template.",
                }

        flask_app = app_module.create_app(runtime=FakeRuntime())
        response = flask_app.test_client().get("/betting")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No market assessments yet", response.data)
        self.assertIn(b"Historical replay payload reached the template.", response.data)
        self.assertEqual(response.headers["X-Betting-Replay-Status"], "ready")

    def test_answer_endpoint_returns_payload_from_answer_service(self):
        class FakeAnswerService:
            def __init__(self, *args, **kwargs):
                pass

            def answer(self, query, source="wikipedia", limit=10, session_id=None):
                return {
                    "query": query,
                    "source": source,
                    "answer": "stubbed answer",
                }

        original = app_module.AnswerService
        app_module.AnswerService = FakeAnswerService
        try:
            flask_app = app_module.create_app()
            client = flask_app.test_client()
            response = client.get("/api/answer?q=test")
        finally:
            app_module.AnswerService = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["answer"], "stubbed answer")


if __name__ == "__main__":
    unittest.main()
