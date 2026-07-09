import requests

from config import Config


def embed(text: str) -> list[float]:
    """
    Request an embedding from the HP embedding service.
    """

    if not Config.EMBEDDING_SERVICE_URL:
        raise RuntimeError(
            "EMBEDDING_SERVICE_URL is not configured."
        )

    response = requests.post(
        Config.EMBEDDING_SERVICE_URL,
        json={"text": text},
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()

    print("Payload:", payload)

    if "embedding" not in payload:
        raise RuntimeError(
            "Embedding service returned an invalid response."
        )

    print("Embedding type:", type(payload["embedding"]))

    return payload["embedding"]
