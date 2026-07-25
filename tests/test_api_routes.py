from api.routes import combined


def test_combined_search_and_retrieve_endpoint_returns_both_payloads(monkeypatch):
    class FakeSearchService:
        def search(self, source, query, limit=10):
            return []

    class FakeRetrievalService:
        def retrieve(self, query):
            return []

    monkeypatch.setattr("api.routes.SearchService", lambda *args, **kwargs: FakeSearchService())
    monkeypatch.setattr("api.routes.RetrievalService", lambda *args, **kwargs: FakeRetrievalService())

    payload = combined(q="test")

    assert payload["query"] == "test"
    assert payload["search"]["results"] == []
    assert payload["retrieve"]["results"] == []
