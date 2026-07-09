try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - optional dependency
    TestClient = None

from api.routes import router


if TestClient is not None:
    client = TestClient(router)
else:
    client = None


def test_combined_search_and_retrieve_endpoint_returns_both_payloads(monkeypatch):
    if client is None:
        return
    class FakeSearchService:
        def search(self, source, query, limit=10):
            return []

    class FakeRetrievalService:
        def retrieve(self, query):
            return []

    monkeypatch.setattr("api.routes.SearchService", lambda *args, **kwargs: FakeSearchService())
    monkeypatch.setattr("api.routes.RetrievalService", lambda *args, **kwargs: FakeRetrievalService())

    response = client.get("/combined?q=test")

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "test"
    assert payload["search"]["results"] == []
    assert payload["retrieve"]["results"] == []
