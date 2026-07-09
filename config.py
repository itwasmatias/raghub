import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # -------------------------
    # PostgreSQL
    # -------------------------
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = int(os.getenv("DB_PORT", "5432"))
    DB_NAME = os.getenv("DB_NAME")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")

    # -------------------------
    # OpenAI
    # -------------------------
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

    CHAT_MODEL = os.getenv(
        "CHAT_MODEL",
        "gpt-4.1-mini",
    )

    # -------------------------
    # Embedding Microservice
    # -------------------------
    EMBEDDING_SERVICE_URL = os.getenv(
        "EMBEDDING_SERVICE_URL"
    )

    # -------------------------
    # Retrieval
    # -------------------------
    TOP_K = int(
        os.getenv("TOP_K", "5")
    )

    MAX_HISTORY = int(
        os.getenv("MAX_HISTORY", "10")
    )
