from models.search_result import SearchResult


def test_search_result_supports_content_type_and_metadata():
    result = SearchResult(
        title="Example",
        url="https://example.com",
        snippet="Example snippet",
        source="wikipedia",
        content_type="text/plain",
        metadata={"lang": "en"},
    )

    assert result.content_type == "text/plain"
    assert result.metadata == {"lang": "en"}


def test_search_result_defaults_keep_existing_connectors_working():
    result = SearchResult(
        title="Example",
        url="https://example.com",
        snippet="Example snippet",
        source="wikipedia",
    )

    assert result.content_type is None
    assert result.metadata == {}
