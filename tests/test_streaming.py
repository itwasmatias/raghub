import unittest

import app as app_module


class StreamingTests(unittest.TestCase):
    def test_streaming_answer_endpoint_returns_chunked_response(self):
        class FakeAnswerService:
            def __init__(self, *args, **kwargs):
                pass

            def stream_answer(self, query, source="wikipedia", limit=10, session_id=None):
                yield "chunk-1"
                yield " chunk-2"

        original = app_module.AnswerService
        app_module.AnswerService = FakeAnswerService
        try:
            flask_app = app_module.create_app()
            client = flask_app.test_client()
            response = client.get("/api/answer/stream?q=test")
        finally:
            app_module.AnswerService = original

        self.assertEqual(response.status_code, 200)
        self.assertIn("chunk-1", response.get_data(as_text=True))
        self.assertIn("chunk-2", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
