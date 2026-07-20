from models.evidence import Evidence
from services.deduplicator import Deduplicator


def test_duplicate_urls_removed():
    evidence = [
        Evidence(
            title="A",
            snippet="",
            content="",
            source="wiki",
            url="https://example.com",
        ),
        Evidence(
            title="B",
            snippet="",
            content="",
            source="github",
            url="https://example.com",
        ),
    ]

    result = Deduplicator.deduplicate(evidence)

    assert len(result) == 1

def test_duplicate_title_source_removed():
    evidence = [
        Evidence(
            title="AI",
            snippet="",
            content="",
            source="wiki",
        ),
        Evidence(
            title="AI",
            snippet="",
            content="",
            source="wiki",
        ),
    ]

    result = Deduplicator.deduplicate(evidence)

    assert len(result) == 1

def test_different_sources_kept():
    evidence = [
        Evidence(
            title="AI",
            snippet="",
            content="",
            source="wiki",
        ),
        Evidence(
            title="AI",
            snippet="",
            content="",
            source="github",
        ),
    ]

    result = Deduplicator.deduplicate(evidence)

    assert len(result) == 2

def test_empty():
    assert Deduplicator.deduplicate([]) == []        