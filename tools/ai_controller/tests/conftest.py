from __future__ import annotations

import json
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest


@dataclass
class FakeOllamaState:
    version_delay: float = 0.0
    tags_models: list[dict[str, Any]] = field(default_factory=lambda: [{"name": "qwen2.5-coder:3b"}])
    generate_queue: list[dict[str, Any]] = field(default_factory=list)
    chat_queue: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[dict[str, Any]] = field(default_factory=list)
    generate_requests: list[dict[str, Any]] = field(default_factory=list)
    chat_requests: list[dict[str, Any]] = field(default_factory=list)


class FakeOllamaServer(AbstractContextManager["FakeOllamaServer"]):
    def __init__(self, **kwargs: Any) -> None:
        self.state = FakeOllamaState(**kwargs)

        class Handler(BaseHTTPRequestHandler):
            server: "ThreadingHTTPServer"

            def log_message(self, *_: Any) -> None:  # pragma: no cover - noisy server logs
                return

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_raw(self, status: int, raw: str) -> None:
                body = raw.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self.server.state.request_log.append({"method": "GET", "path": self.path})
                if self.path == "/api/version":
                    if self.server.state.version_delay:
                        time.sleep(self.server.state.version_delay)
                    self._send_json(200, {"version": "test"})
                    return
                if self.path == "/api/tags":
                    self._send_json(200, {"models": self.server.state.tags_models})
                    return
                self._send_json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                self.server.state.request_log.append(
                    {"method": "POST", "path": self.path, "body": body}
                )
                if self.path == "/api/generate":
                    self.server.state.generate_requests.append(json.loads(body or "{}"))
                    if not self.server.state.generate_queue:
                        self._send_json(200, {"response": "{}"})
                        return
                    response = self.server.state.generate_queue.pop(0)
                    delay = response.get("delay", 0.0)
                    if delay:
                        time.sleep(delay)
                    status = response.get("status", 200)
                    if "raw" in response:
                        self._send_raw(status, response["raw"])
                        return
                    self._send_json(status, response.get("body", {}))
                    return
                if self.path == "/api/chat":
                    parsed = json.loads(body or "{}")
                    self.server.state.chat_requests.append(parsed)
                    if not self.server.state.chat_queue:
                        # Default: empty finish response
                        content = json.dumps({"thought_summary": "done", "action": {"tool": "finish", "arguments": {"success": False, "summary": "no response queued"}}})
                        self._send_json(200, {"message": {"role": "assistant", "content": content}})
                        return
                    response = self.server.state.chat_queue.pop(0)
                    delay = response.get("delay", 0.0)
                    if delay:
                        time.sleep(delay)
                    status = response.get("status", 200)
                    if "raw" in response:
                        self._send_raw(status, response["raw"])
                        return
                    body_payload = response.get("body", {})
                    if "content" in response:
                        body_payload = {"message": {"role": "assistant", "content": response["content"]}}
                    self._send_json(status, body_payload)
                    return
                self._send_json(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOllamaServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"


@pytest.fixture
def start_ollama_server() -> Iterator[Any]:
    def factory(**kwargs: Any) -> FakeOllamaServer:
        return FakeOllamaServer(**kwargs)

    yield factory
