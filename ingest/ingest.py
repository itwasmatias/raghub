from database import get_connection
from ingest.chunker import chunk_text
from ingest.embed import embed


def ingest_document(title: str, source: str, text: str):

    conn = get_connection()

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO documents(title, source)
        VALUES (%s,%s)
        RETURNING id
        """,
        (title, source),
    )

    document_id = cur.fetchone()[0]

    chunks = chunk_text(text)

    for index, chunk in enumerate(chunks):

        embedding = embed(chunk)

        cur.execute(
            """
            INSERT INTO chunks(
                document_id,
                chunk_index,
                content,
                embedding
            )
            VALUES (%s,%s,%s,%s)
            """,
            (
                document_id,
                index,
                chunk,
                embedding,
            ),
        )

    conn.commit()

    cur.close()

    conn.close()
