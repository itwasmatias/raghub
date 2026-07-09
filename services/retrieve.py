from database import get_connection
from services.embedding_client import embed
from config import Config


def retrieve(query: str):
    """
    Retrieve the most relevant document chunks
    using pgvector cosine similarity.
    """

    query_embedding = embed(query)

    with get_connection() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    document_id,
                    chunk_index,
                    content,
                    embedding <=> %s::vector AS distance
                FROM chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (
                    query_embedding,
                    query_embedding,
                    Config.TOP_K,
                ),
            )

            rows = cur.fetchall()

    return [
        {
            "document_id": row[0],
            "chunk_index": row[1],
            "content": row[2],
            "distance": row[3],
        }
        for row in rows
    ]
