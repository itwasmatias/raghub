"""
Answer orchestration service.
"""

from models.search_result import SearchResult
from services.generator import AnswerGenerator
from services.prompt import PromptBuilder
from services.ranking import RankingService
from services.retrieve import RetrievalService
from services.search import SearchService


class ConversationMemory(dict):
    """Simple in-memory conversation store keyed by session id."""

    pass


class AnswerService:
    """
    Combine search and retrieval results into a unified answer payload.
    """

    def __init__(
        self,
        search_service: SearchService | None = None,
        retrieval_service: RetrievalService | None = None,
        ranking_service: RankingService | None = None,
        prompt_builder: PromptBuilder | None = None,
        generator: AnswerGenerator | None = None,
        conversation_memory: dict | None = None,
    ):
        self.search_service = search_service or SearchService()
        self.retrieval_service = retrieval_service or RetrievalService()
        self.ranking_service = ranking_service or RankingService()
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.generator = generator or AnswerGenerator()
        self.conversation_memory = conversation_memory or ConversationMemory()

    def answer(self, query: str, source: str = "wikipedia", limit: int = 10, session_id: str | None = None) -> dict:
        try:
            search_results = self.search_service.search(source=source, query=query, limit=limit)
        except Exception:
            search_results = []

        try:
            retrieval_results = self.retrieval_service.retrieve(query)
        except Exception:
            retrieval_results = []

        ranked_retrieval = self.ranking_service.rank(
            [
                SearchResult(
                    title=item.content,
                    url="",
                    snippet=item.content,
                    source=source,
                    score=item.score,
                    content_type=None,
                    metadata=item.metadata,
                )
                for item in retrieval_results
            ]
        )

        conversation_history = []
        if session_id:
            conversation_history = list(self.conversation_memory.get(session_id, []))

        prompt = self.prompt_builder.build(
            query=query,
            search_results=search_results,
            retrieval_results=retrieval_results,
            conversation_history=conversation_history,
        )

        answer_text = self.generator.generate(prompt)

        return {
            "query": query,
            "source": source,
            "prompt": prompt,
            "answer": answer_text,
            "conversation_history": conversation_history,
            "search_results": [
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "source": result.source,
                    "score": result.score,
                    "content_type": result.content_type,
                    "metadata": result.metadata,
                }
                for result in search_results
            ],
            "retrieval_results": [
                {
                    "document_id": item.document_id,
                    "chunk_index": item.chunk_index,
                    "content": item.content,
                    "score": item.score,
                    "metadata": item.metadata,
                }
                for item in retrieval_results
            ],
            "ranked_retrieval": [
                {
                    "title": result.title,
                    "score": result.score,
                    "source": result.source,
                    "snippet": result.snippet,
                }
                for result in ranked_retrieval
            ],
        }

    def stream_answer(self, query: str, source: str = "wikipedia", limit: int = 10, session_id: str | None = None):
        payload = self.answer(query=query, source=source, limit=limit, session_id=session_id)
        answer_text = payload.get("answer", "")
        if not answer_text:
            yield ""
            return

        chunk_size = 20
        for index in range(0, len(answer_text), chunk_size):
            yield answer_text[index:index + chunk_size]
