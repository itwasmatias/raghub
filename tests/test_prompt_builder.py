import unittest

from models.retrieval_result import RetrievalResult
from models.search_result import SearchResult
from services.prompt import PromptBuilder


class PromptBuilderTests(unittest.TestCase):
    def test_builds_a_formatted_research_prompt(self):
        builder = PromptBuilder()
        prompt = builder.build(
            query="What is RAGHub?",
            search_results=[
                SearchResult(
                    title="Wikipedia",
                    url="https://example.com/wiki",
                    snippet="A retrieval system",
                    source="wikipedia",
                    score=0.9,
                )
            ],
            retrieval_results=[
                RetrievalResult(
                    document_id=1,
                    chunk_index=0,
                    content="RAGHub is a retrieval system.",
                    score=0.8,
                )
            ],
        )

        self.assertIn("What is RAGHub?", prompt)
        self.assertIn("Search Results", prompt)
        self.assertIn("Retrieval Results", prompt)
        self.assertIn("Wikipedia", prompt)
        self.assertIn("RAGHub is a retrieval system.", prompt)


if __name__ == "__main__":
    unittest.main()
