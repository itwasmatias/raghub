"""
Prompt construction utilities.
"""

from __future__ import annotations

from models.retrieval_result import RetrievalResult
from models.search_result import SearchResult


class PromptBuilder:
    """
    Build a simple, extensible research prompt from search and retrieval context.
    """

    def build(
        self,
        query: str,
        search_results: list[SearchResult],
        retrieval_results: list[RetrievalResult],
        conversation_history: list[dict] | None = None,
    ) -> str:
        sections: list[str] = []
        sections.append(f"User Query: {query}")
        sections.append("")
        sections.append("Search Results")
        sections.append("-" * 12)

        if search_results:
            for result in search_results:
                sections.append(
                    f"- Title: {result.title}\n"
                    f"  Source: {result.source}\n"
                    f"  Snippet: {result.snippet}\n"
                    f"  Score: {result.score}"
                )
        else:
            sections.append("- None")

        if conversation_history:
            sections.append("")
            sections.append("Conversation History")
            sections.append("-" * 20)
            for turn in conversation_history:
                user_text = turn.get("user", "")
                assistant_text = turn.get("assistant", "")
                sections.append(f"- User: {user_text}")
                sections.append(f"  Assistant: {assistant_text}")

        sections.append("")
        sections.append("Retrieval Results")
        sections.append("-" * 16)

        if retrieval_results:
            for result in retrieval_results:
                sections.append(
                    f"- Document ID: {result.document_id}\n"
                    f"  Chunk Index: {result.chunk_index}\n"
                    f"  Content: {result.content}\n"
                    f"  Score: {result.score}"
                )
        else:
            sections.append("- None")

        sections.append("")
        sections.append("Instructions: Answer the user query using the provided context.")
        return "\n".join(sections)
