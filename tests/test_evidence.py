from models.evidence import Evidence


def test_evidence_creation():
    evidence = Evidence(
        title="AI",
        snippet="Artificial Intelligence",
        content="Long content",
        source="wikipedia",
    )

    assert evidence.title == "AI"
    assert evidence.source == "wikipedia"