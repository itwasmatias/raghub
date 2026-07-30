from __future__ import annotations

from tools.ai_controller.providers import LocalOllamaProvider
from tools.ai_controller.remote import LocalRunner


def test_local_ollama_provider_uses_configured_model():
    provider = LocalOllamaProvider(
        runner=LocalRunner(),
        base_url="http://127.0.0.1:11434",
        model="qwen2.5-coder:7b",
        request_timeout_seconds=1,
        generation_timeout_seconds=2,
        context_size=4096,
        temperature=0.1,
        max_retries=2,
        max_output_chars=4096,
    )

    assert provider.name == "local-ollama"
    assert provider.model == "qwen2.5-coder:7b"
    assert provider.base_url == "http://127.0.0.1:11434/"
    assert provider.request_timeout_seconds == 1
    assert provider.generation_timeout_seconds == 2
    assert provider.context_size == 4096
    assert provider.temperature == 0.1
    assert provider.max_retries == 2
    assert provider.max_output_chars == 4096
