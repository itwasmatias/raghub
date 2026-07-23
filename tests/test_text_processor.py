from analytics.text.processor import TextProcessor


def test_text_processor_normalizes_text():
    processor = TextProcessor()

    text = "The AI industry is growing!"

    tokens = processor.process(text)

    assert "ai" in tokens
    assert "industry" in tokens
    assert "growing" in tokens

    assert "the" not in tokens
    assert "is" not in tokens
    assert "growing!" not in tokens