"""
Generative answer service.
"""

from __future__ import annotations

import os

from config import Config


class AnswerGenerator:
    """
    Generate an answer from a formatted prompt.
    This service is intentionally separate from prompt construction and orchestration.
    """

    def _fallback_answer(self, prompt: str) -> str:
        if not prompt.strip():
            return ""

        lines = [line.strip() for line in prompt.splitlines() if line.strip()]
        query_line = next((line for line in lines if line.startswith("User Query:")), None)
        query = query_line.split(":", 1)[1].strip() if query_line else "your question"

        return (
            f"I could not reach an external model, so I synthesized a local answer for: {query}. "
            "Please verify any factual claims against the retrieved context."
        )

    def generate(self, prompt: str) -> str:
        if not prompt.strip():
            return ""

        use_openai = os.getenv("RAGHUB_USE_OPENAI", "").lower() in {"1", "true", "yes", "on"}
        if not use_openai or not Config.OPENAI_API_KEY:
            return self._fallback_answer(prompt)

        try:
            from openai import OpenAI
        except ImportError:
            return self._fallback_answer(prompt)

        try:
            client = OpenAI(api_key=Config.OPENAI_API_KEY)
            response = client.responses.create(
                model=Config.CHAT_MODEL,
                input=prompt,
            )
            text = getattr(response, "output_text", None)
            if isinstance(text, str) and text.strip():
                return text
        except Exception:
            return self._fallback_answer(prompt)

        return self._fallback_answer(prompt)
