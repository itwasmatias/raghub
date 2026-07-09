import os

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    def load_dotenv() -> None:
        return None

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency
    psycopg = None

load_dotenv()


def get_connection():
    if psycopg is None:
        raise RuntimeError("psycopg is not installed")

    return psycopg.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
