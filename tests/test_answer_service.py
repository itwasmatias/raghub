import unittest

from models.retrieval_result import RetrievalResult
from models.search_result import SearchResult
from services.answer import AnswerService


class FakeSearchService:
    def search(self, source, query, limit=10):
        return [
            SearchResult(
                title="Search title",
                url="https://example.com/search",
                snippet="search snippet",
                source=source,
                score=0.9,
            )
        ]


class FakeRetrievalService:
    def retrieve(self, query):
        return [
            RetrievalResult(
                document_id=1,
                chunk_index=0,
                content="Retrieved chunk",
                score=0.8,
            )
        ]


class AnswerServiceTests(unittest.TestCase):
    def test_answer_service_composes_search_and_retrieval_results(self):
        service = AnswerService(
            search_service=FakeSearchService(),
            retrieval_service=FakeRetrievalService(),
        )

        payload = service.answer("test query")

        self.assertEqual(payload["query"], "test query")
        self.assertEqual(payload["search_results"][0]["title"], "Search title")
        self.assertEqual(payload["retrieval_results"][0]["content"], "Retrieved chunk")
        self.assertEqual(payload["ranked_retrieval"][0]["title"], "Retrieved chunk")

    def test_answer_service_falls_back_gracefully_when_search_fails(self):
        class FailingSearchService:
            def search(self, source, query, limit=10):
                raise RuntimeError("search unavailable")

        service = AnswerService(
            search_service=FailingSearchService(),
            retrieval_service=FakeRetrievalService(),
        )

        payload = service.answer("test query")

        self.assertEqual(payload["query"], "test query")
        self.assertEqual(payload["search_results"], [])
        self.assertEqual(payload["retrieval_results"][0]["content"], "Retrieved chunk")
        self.assertIn("answer", payload)
        self.assertIsInstance(payload["answer"], str)

    def test_answer_service_uses_conversation_history_in_prompt(self):
        class RecordingPromptBuilder:
            def __init__(self):
                self.last_prompt = ""

            def build(self, query, search_results, retrieval_results, conversation_history=None):
                self.last_prompt = "|".join([query, str(search_results), str(retrieval_results), str(conversation_history)])
                return self.last_prompt

        class RecordingGenerator:
            def generate(self, prompt):
                return prompt

        prompt_builder = RecordingPromptBuilder()
        generator = RecordingGenerator()

        service = AnswerService(
            search_service=FakeSearchService(),
            retrieval_service=FakeRetrievalService(),
            prompt_builder=prompt_builder,
            generator=generator,
            conversation_memory={"session-1": [{"user": "Hello", "assistant": "Hi"}]},
        )

        payload = service.answer("test query", session_id="session-1")

        self.assertIn("Hello", payload["prompt"])
        self.assertIn("Hi", payload["prompt"])
        self.assertEqual(payload["conversation_history"][0]["user"], "Hello")


if __name__ == "__main__":
    unittest.main()
