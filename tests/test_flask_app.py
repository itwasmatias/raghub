import unittest

import app as app_module


class FlaskAppTests(unittest.TestCase):
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
