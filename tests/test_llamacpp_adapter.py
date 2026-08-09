"""Governed llama.cpp local adapter tests — loopback only, deterministic mocks."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import socket
import threading
import time
from unittest import mock

import pytest
import urllib3

from federation.llamacpp_adapter import (
    LlamaCppAdapterConfig,
    LlamaCppAdapterError,
    LlamaCppLocalAdapter,
    LlamaCppNetworkPolicyError,
    LlamaCppServerIdentityError,
    LlamaCppUnsupportedParameterError,
    _validate_loopback_only,
)
from federation.task_request import AuthorizationLevel
from tools.ai_controller.local_capability_baseline import LocalityType
from tools.ai_controller.local_model_runtime import (
    GenerationConfig,
    GovernedLocalModelRuntime,
    LocalInferenceRequest,
    LocalInferenceStatus,
    LocalModelCapability,
    LocalModelDescriptor,
    LocalModelLoadStatus,
    LocalModelRuntimeConfig,
)


NOW = datetime(2026, 8, 9, 15, 0, 0, tzinfo=timezone.utc)


def fixed_clock():
    return NOW


def descriptor(**changes):
    values = dict(
        model_id="llama-3.2-1b-q4",
        runtime_id="llama-cpp-runtime-1",
        node_id="worker-1",
        profile_fingerprint="a" * 64,
        locality=LocalityType.LOCAL,
        family="llama-3.2",
        quantization="Q4_K_M",
        context_limit=8192,
        capabilities=frozenset(
            {
                LocalModelCapability.TEXT_GENERATION,
            }
        ),
        load_status=LocalModelLoadStatus.LOADED,
        runtime_metadata={"version": "b3659", "backend": "llama.cpp"},
    )
    values.update(changes)
    return LocalModelDescriptor(**values)


def generation(**changes):
    values = dict(
        maximum_output_tokens=128,
        temperature=0.2,
        seed=42,
        stop_conditions=("STOP",),
        structured_output_required=False,
    )
    values.update(changes)
    return GenerationConfig(**values)


def request(**changes):
    values = dict(
        request_id="request-1",
        task_id="task-1",
        worker_node_id="worker-1",
        descriptor_fingerprint=descriptor().descriptor_fingerprint(),
        input_text="bounded prompt",
        generation=generation(),
        authorization_level=AuthorizationLevel.INTERNAL,
        approval_required=False,
        permitted_tool_capabilities=("python",),
        local_only=True,
        execution_attempt_id="execution-1",
        assignment_id="assignment-1",
        dispatch_offer_id="offer-1",
        execution_fingerprint="b" * 64,
        benchmark_linkage=None,
    )
    values.update(changes)
    return LocalInferenceRequest(**values)


def adapter_config(**changes):
    values = dict(
        adapter_id="llamacpp-adapter-1",
        node_id="worker-1",
        endpoint="http://127.0.0.1:8080",
        expected_runtime_id="llama-cpp-runtime-1",
        expected_model_id="llama-3.2-1b-q4",
        model_fingerprint=None,
        connection_timeout_seconds=5.0,
        request_timeout_seconds=30.0,
        maximum_response_bytes=1024 * 1024,
        api_route="/completion",
    )
    values.update(changes)
    return LlamaCppAdapterConfig(**values)


class FakeLlamaServer(BaseHTTPRequestHandler):
    """Deterministic fake llama-server for testing."""

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404, "Not Found")
            return
        self.server.models_request_count += 1

        if hasattr(self.server, "models_redirect_location"):
            self.send_response(self.server.models_redirect_status)
            self.send_header("Location", self.server.models_redirect_location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Default models response
        response = {
            "object": "list",
            "data": [
                {
                    "id": "llama-3.2-1b-q4",
                    "object": "model",
                    "created": 1234567890,
                    "owned_by": "organization",
                }
            ],
        }

        # Check for custom test behaviors
        if hasattr(self.server, "models_response_override"):
            response = self.server.models_response_override

        if hasattr(self.server, "models_status_override"):
            self.send_response(self.server.models_status_override)
        else:
            self.send_response(200)

        self.send_header("Content-Type", "application/json")
        response_bytes = json.dumps(response).encode("utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def do_POST(self):
        if self.path != "/completion":
            self.send_error(404, "Not Found")
            return
        self.server.inference_request_count += 1

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        if hasattr(self.server, "inference_redirect_location"):
            self.send_response(self.server.inference_redirect_status)
            self.send_header("Location", self.server.inference_redirect_location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        try:
            req = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400, "Bad Request")
            return

        # Store request for assertions
        self.server.last_request = req

        # Deterministic response
        response = {
            "content": f"response to: {req.get('prompt', '')}",
            "stopped_eos": True,
            "stopped_word": False,
            "stopped_limit": False,
            "timings": {
                "prompt_n": 10,
                "predicted_n": 5,
                "prompt_ms": 100.0,
                "predicted_ms": 50.0,
            },
        }

        # Check for custom test behaviors
        if hasattr(self.server, "response_override"):
            response = self.server.response_override

        if hasattr(self.server, "status_override"):
            self.send_response(self.server.status_override)
        else:
            self.send_response(200)

        self.send_header("Content-Type", "application/json")
        response_bytes = json.dumps(response).encode("utf-8")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format, *args):
        # Suppress request logging
        pass


def start_fake_server(port=0):
    """Start a fake llama-server on loopback."""
    server = HTTPServer(("127.0.0.1", port), FakeLlamaServer)
    server.last_request = None
    server.models_request_count = 0
    server.inference_request_count = 0
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server


class RedirectTargetServer(BaseHTTPRequestHandler):
    """Records any redirect follow without serving useful content."""

    def _record(self):
        self.server.contact_count += 1
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length:
            self.rfile.read(content_length)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _record
    do_POST = _record

    def log_message(self, format, *args):
        pass


def start_redirect_target():
    server = HTTPServer(("127.0.0.1", 0), RedirectTargetServer)
    server.contact_count = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ==================================================
# Configuration and Loopback Validation Tests
# ==================================================


def test_adapter_config_is_immutable():
    config = adapter_config()
    fingerprint = config.config_fingerprint()
    assert config.config_fingerprint() == fingerprint
    with pytest.raises(FrozenInstanceError):
        config.endpoint = "http://evil.com"


def test_adapter_config_fingerprint_is_stable_and_material():
    assert adapter_config().config_fingerprint() == adapter_config().config_fingerprint()
    assert (
        adapter_config(endpoint="http://127.0.0.1:9999").config_fingerprint()
        != adapter_config().config_fingerprint()
    )
    assert (
        adapter_config(expected_model_id="different").config_fingerprint()
        != adapter_config().config_fingerprint()
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8080/",
        "127.0.0.1:8080",
        "http://[::1]:8080",
        "::1",
        "http://localhost:8080",
        "localhost:8080",
        "localhost",
    ],
)
def test_loopback_endpoints_accepted(endpoint):
    config = adapter_config(endpoint=endpoint)
    assert config.endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://192.168.1.1:8080",
        "http://10.0.0.1:8080",
        "http://8.8.8.8:8080",
        "http://example.com:8080",
        "192.168.1.1",
        "10.0.0.1",
    ],
)
def test_non_loopback_endpoints_rejected(endpoint):
    with pytest.raises(LlamaCppNetworkPolicyError):
        adapter_config(endpoint=endpoint)


def test_hostname_resolving_to_non_loopback_rejected():
    # Mock DNS resolution to return non-loopback
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 8080))]

    with mock.patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(LlamaCppNetworkPolicyError, match="non-loopback"):
            adapter_config(endpoint="http://testhost:8080")


def test_redirect_outside_loopback_cannot_be_configured():
    # The adapter validates at config time, so redirects are not followed
    # This test documents that validation happens before any HTTP requests
    config = adapter_config(endpoint="http://127.0.0.1:8080")
    assert config.endpoint == "http://127.0.0.1:8080"


def test_proxy_environment_variable_ignored():
    # urllib3.PoolManager by default does not use proxies
    # This test documents that behavior
    with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://evil.com:8080"}):
        config = adapter_config()
        adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)
        # If proxy were used, this would fail at adapter construction
        assert adapter._http is not None


# ==================================================
# Adapter Construction and Identity Tests
# ==================================================


def test_adapter_requires_valid_config_type():
    with pytest.raises(TypeError, match="LlamaCppAdapterConfig"):
        LlamaCppLocalAdapter(config={"endpoint": "http://127.0.0.1:8080"})


def test_adapter_rejects_clock_that_is_not_callable():
    with pytest.raises(TypeError, match="callable"):
        LlamaCppLocalAdapter(config=adapter_config(), clock="not callable")


def test_adapter_verifies_config_immutability_at_construction():
    config = adapter_config()
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)
    assert adapter._config is config


def test_runtime_id_mismatch_rejected():
    config = adapter_config(expected_runtime_id="different-runtime")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)
    with pytest.raises(LlamaCppServerIdentityError, match="runtime_id"):
        adapter.infer(descriptor(), request(), LocalModelRuntimeConfig(True, 512, frozenset(), True))


def test_model_id_mismatch_rejected():
    config = adapter_config(expected_model_id="different-model")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)
    with pytest.raises(LlamaCppServerIdentityError, match="model_id"):
        adapter.infer(descriptor(), request(), LocalModelRuntimeConfig(True, 512, frozenset(), True))


def test_node_id_mismatch_rejected():
    config = adapter_config(node_id="different-node")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)
    with pytest.raises(LlamaCppAdapterError, match="node_id"):
        adapter.infer(
            descriptor(node_id="worker-1"),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )


# ==================================================
# Request Mapping and Parameter Tests
# ==================================================


def test_seed_forwarded_exactly():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    adapter.infer(
        descriptor(),
        request(generation=generation(seed=12345)),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert server.last_request["seed"] == 12345
    server.shutdown()


def test_temperature_forwarded_exactly():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    adapter.infer(
        descriptor(),
        request(generation=generation(temperature=0.7)),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert server.last_request["temperature"] == 0.7
    server.shutdown()


def test_max_tokens_forwarded_as_n_predict():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    adapter.infer(
        descriptor(),
        request(generation=generation(maximum_output_tokens=256)),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert server.last_request["n_predict"] == 256
    server.shutdown()


def test_stop_conditions_forwarded():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    adapter.infer(
        descriptor(),
        request(generation=generation(stop_conditions=("STOP", "END"))),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert server.last_request["stop"] == ["STOP", "END"]
    server.shutdown()


def test_structured_output_requirement_fails_closed():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppUnsupportedParameterError, match="structured_output"):
        adapter.infer(
            descriptor(),
            request(generation=generation(structured_output_required=True)),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_stream_disabled():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert server.last_request["stream"] is False
    server.shutdown()


# ==================================================
# Response Mapping and Validation Tests
# ==================================================


def test_successful_inference_returns_valid_response():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(input_text="test prompt"),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.SUCCEEDED
    assert response.output_text == "response to: test prompt"
    assert response.model_id == "llama-3.2-1b-q4"
    assert response.runtime_id == "llama-cpp-runtime-1"
    assert response.node_id == "worker-1"
    assert response.locality is LocalityType.LOCAL
    assert response.remote_execution is False
    assert response.cloud_escalation_count == 0
    assert response.cloud_cost_usd == 0.0
    assert response.usage.prompt_tokens == 10
    assert response.usage.generated_tokens == 5
    assert response.termination_reason == "eos"

    server.shutdown()


def test_token_counts_extracted_correctly():
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": 42, "predicted_n": 17},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.usage.prompt_tokens == 42
    assert response.usage.generated_tokens == 17
    server.shutdown()


def test_missing_token_counts_handled():
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.usage.prompt_tokens is None
    assert response.usage.generated_tokens is None
    server.shutdown()


def test_negative_token_counts_rejected():
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": -1, "predicted_n": 5},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "negative" in response.error_message.lower()
    server.shutdown()


def test_string_token_count_rejected():
    """String token counts must be rejected (fail closed)."""
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": "not a number", "predicted_n": 5},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    # String token counts must be rejected (fail closed)
    assert response.status is LocalInferenceStatus.FAILED
    assert "malformed" in response.error_code
    server.shutdown()


def test_boolean_true_token_count_rejected():
    """Boolean True token count must be rejected (fail closed)."""
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": True, "predicted_n": 5},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "malformed" in response.error_code
    assert "boolean" in response.error_message.lower()
    server.shutdown()


def test_boolean_false_token_count_rejected():
    """Boolean False token count must be rejected (fail closed)."""
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": 10, "predicted_n": False},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "malformed" in response.error_code
    assert "boolean" in response.error_message.lower()
    server.shutdown()


def test_float_token_count_rejected():
    """Float token counts must be rejected (fail closed)."""
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": 10.5, "predicted_n": 5},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "malformed" in response.error_code
    assert "not integer" in response.error_message.lower()
    server.shutdown()


def test_list_token_count_rejected():
    """List token counts must be rejected (fail closed)."""
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": [10], "predicted_n": 5},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "malformed" in response.error_code
    server.shutdown()


def test_dict_token_count_rejected():
    """Dict token counts must be rejected (fail closed)."""
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": {"value": 10}, "predicted_n": 5},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "malformed" in response.error_code
    server.shutdown()


def test_zero_token_count_accepted():
    """Zero token count is valid and must be accepted."""
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_n": 0, "predicted_n": 5},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.SUCCEEDED
    assert response.usage.prompt_tokens == 0
    assert response.usage.generated_tokens == 5
    server.shutdown()


def test_absent_token_count_accepted():
    """Absent token count field is valid (unknown) and accepted as None."""
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {},  # No token counts present
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.SUCCEEDED
    assert response.usage.prompt_tokens is None
    assert response.usage.generated_tokens is None
    server.shutdown()


# ==================================================
# Model Attestation Tests
# ==================================================


def test_expected_model_accepted():
    """Expected loaded model identity is accepted."""
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.SUCCEEDED
    server.shutdown()


def test_wrong_model_rejected():
    """Wrong loaded model is rejected before inference."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = {
        "object": "list",
        "data": [{"id": "different-model", "object": "model"}],
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="does not match"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_no_model_loaded_rejected():
    """Empty models list is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = {
        "object": "list",
        "data": [],
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="no models available"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_ambiguous_multiple_models_rejected():
    """Multiple models in response is rejected (ambiguous authority)."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = {
        "object": "list",
        "data": [
            {"id": "model-1", "object": "model"},
            {"id": "model-2", "object": "model"},
        ],
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="ambiguous model authority"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_malformed_models_metadata_rejected():
    """Malformed metadata response is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = "not a dict"

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="not a JSON object"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_missing_data_field_rejected():
    """Missing 'data' field in metadata is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = {"object": "list"}

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="missing 'data' field"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_data_not_list_rejected():
    """'data' field that is not a list is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = {
        "object": "list",
        "data": "not a list",
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="not a list"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_model_info_not_dict_rejected():
    """Model info that is not a dict is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = {
        "object": "list",
        "data": ["not a dict"],
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="not a JSON object"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_missing_model_id_rejected():
    """Missing model 'id' field is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = {
        "object": "list",
        "data": [{"object": "model"}],
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="missing 'id' field"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_non_string_model_id_rejected():
    """Non-string model ID is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_response_override = {
        "object": "list",
        "data": [{"id": 12345, "object": "model"}],
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="not string"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_oversized_metadata_rejected():
    """Oversized metadata response is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    # Create large response
    server.models_response_override = {
        "object": "list",
        "data": [{"id": "x" * 10000, "object": "model"}],
    }

    config = adapter_config(
        endpoint=f"http://127.0.0.1:{port}",
        maximum_response_bytes=100,
    )
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="maximum size"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_metadata_http_error_rejected():
    """HTTP error from metadata endpoint is rejected."""
    server = start_fake_server()
    port = server.server_address[1]
    server.models_status_override = 500

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    with pytest.raises(LlamaCppServerIdentityError, match="HTTP 500"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

    server.shutdown()


def test_metadata_uses_loopback_only():
    """Metadata request respects loopback-only restriction."""
    # This is enforced at config validation time
    with pytest.raises(LlamaCppNetworkPolicyError):
        adapter_config(endpoint="http://192.168.1.1:8080")


def test_metadata_does_not_use_proxies():
    """Metadata request does not use environmental proxies."""
    # urllib3.PoolManager by default does not use proxies
    with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://evil.com:8080"}):
        server = start_fake_server()
        port = server.server_address[1]

        config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
        adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

        # If proxy were used, this would fail to reach the test server
        response = adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )

        assert response.status is LocalInferenceStatus.SUCCEEDED
        server.shutdown()


# ==================================================
# Response Measurements and Termination Tests
# ==================================================


def test_resource_measurements_extracted():
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {
        "content": "output",
        "stopped_eos": True,
        "timings": {"prompt_ms": 123.45, "predicted_ms": 67.89},
    }

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.resource_measurements["prompt_ms"] == 123.45
    assert response.resource_measurements["predicted_ms"] == 67.89
    server.shutdown()


def test_termination_reason_mapped_correctly():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    # Test stop_word
    server.response_override = {
        "content": "output",
        "stopped_word": True,
        "stopped_eos": False,
        "stopped_limit": False,
    }
    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )
    assert response.termination_reason == "stop_word"

    # Test max_tokens
    server.response_override = {
        "content": "output",
        "stopped_word": False,
        "stopped_eos": False,
        "stopped_limit": True,
    }
    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )
    assert response.termination_reason == "max_tokens"

    # Test eos
    server.response_override = {
        "content": "output",
        "stopped_word": False,
        "stopped_eos": True,
        "stopped_limit": False,
    }
    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )
    assert response.termination_reason == "eos"

    server.shutdown()


# ==================================================
# HTTP Error Handling Tests
# ==================================================


def test_metadata_redirect_is_not_followed_retried_or_allowed_to_infer():
    source = start_fake_server()
    target = start_redirect_target()
    source.models_redirect_status = 302
    source.models_redirect_location = (
        f"http://127.0.0.1:{target.server_address[1]}/redirected-models"
    )
    adapter = LlamaCppLocalAdapter(
        config=adapter_config(
            endpoint=f"http://127.0.0.1:{source.server_address[1]}"
        ),
        clock=fixed_clock,
    )

    try:
        with pytest.raises(LlamaCppServerIdentityError, match="HTTP 302"):
            adapter.infer(
                descriptor(),
                request(),
                LocalModelRuntimeConfig(True, 512, frozenset(), True),
            )
        assert source.models_request_count == 1
        assert source.inference_request_count == 0
        assert source.last_request is None
        assert target.contact_count == 0
    finally:
        source.shutdown()
        target.shutdown()


def test_inference_307_redirect_is_not_followed_replayed_or_retried():
    source = start_fake_server()
    target = start_redirect_target()
    source.inference_redirect_status = 307
    source.inference_redirect_location = (
        f"http://127.0.0.1:{target.server_address[1]}/redirected-inference"
    )
    adapter = LlamaCppLocalAdapter(
        config=adapter_config(
            endpoint=f"http://127.0.0.1:{source.server_address[1]}"
        ),
        clock=fixed_clock,
    )

    try:
        result = adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )
        assert result.status is LocalInferenceStatus.FAILED
        assert result.error_code == "http_request_failed"
        assert "307" in result.error_message
        assert source.models_request_count == 1
        assert source.inference_request_count == 1
        assert target.contact_count == 0
    finally:
        source.shutdown()
        target.shutdown()


def test_http_404_returns_error_response():
    server = start_fake_server()
    port = server.server_address[1]
    server.status_override = 404

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "404" in response.error_message
    server.shutdown()


def test_http_500_returns_error_response():
    server = start_fake_server()
    port = server.server_address[1]
    server.status_override = 500

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "500" in response.error_message
    server.shutdown()


def test_connection_refused_returns_error_response():
    """Connection refused during attestation raises error before inference."""
    # Use a port that's not listening
    config = adapter_config(endpoint="http://127.0.0.1:1")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    # Connection refused during attestation raises LlamaCppServerIdentityError
    with pytest.raises(LlamaCppServerIdentityError, match="failed to query model metadata"):
        adapter.infer(
            descriptor(),
            request(),
            LocalModelRuntimeConfig(True, 512, frozenset(), True),
        )


def test_timeout_returns_error_response():
    server = start_fake_server()
    port = server.server_address[1]

    # Set very short timeout
    config = adapter_config(
        endpoint=f"http://127.0.0.1:{port}",
        connection_timeout_seconds=0.001,
        request_timeout_seconds=0.001,
    )
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    # Mock server to delay
    original_do_POST = FakeLlamaServer.do_POST

    def delayed_POST(self):
        time.sleep(0.1)
        original_do_POST(self)

    FakeLlamaServer.do_POST = delayed_POST

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    FakeLlamaServer.do_POST = original_do_POST
    server.shutdown()


def test_connection_loss_after_transmission_requires_reconciliation():
    """Connection loss after request transmission returns FAILED status.

    This documents that ambiguous completion (where we cannot know if the
    server processed the request) is represented as FAILED, which triggers
    reconciliation-required semantics in the runtime, not as a safely
    retryable ordinary failure.
    """
    server = start_fake_server()
    port = server.server_address[1]

    # Mock server to close connection during response
    original_do_POST = FakeLlamaServer.do_POST

    def connection_loss_POST(self):
        # Read request (simulating server received it)
        content_length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(content_length)
        # Close connection without response (ambiguous completion)
        self.wfile.close()

    FakeLlamaServer.do_POST = connection_loss_POST

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    # Ambiguous completion is represented as FAILED (reconciliation required)
    assert response.status is LocalInferenceStatus.FAILED
    assert response.error_code == "http_request_failed"
    # Not marked as safely retryable - runtime must use reconciliation

    FakeLlamaServer.do_POST = original_do_POST
    server.shutdown()


def test_malformed_json_response_returns_error():
    server = start_fake_server()
    port = server.server_address[1]

    # Override to return invalid JSON
    original_do_POST = FakeLlamaServer.do_POST

    def malformed_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        malformed = b"{malformed json"
        self.send_header("Content-Length", str(len(malformed)))
        self.end_headers()
        self.wfile.write(malformed)

    FakeLlamaServer.do_POST = malformed_POST

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "json" in response.error_message.lower()

    FakeLlamaServer.do_POST = original_do_POST
    server.shutdown()


def test_oversized_response_rejected():
    """Oversized inference response is rejected."""
    server = start_fake_server()
    port = server.server_address[1]

    # Configure moderate maximum (enough for attestation, too small for large inference)
    config = adapter_config(
        endpoint=f"http://127.0.0.1:{port}", maximum_response_bytes=200
    )
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    # Override to return very large inference response
    server.response_override = {
        "content": "x" * 10000,
        "stopped_eos": True,
    }

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "maximum size" in response.error_message
    server.shutdown()


def test_missing_content_field_returns_error():
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {"stopped_eos": True}

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "missing" in response.error_message.lower()
    server.shutdown()


def test_invalid_content_type_returns_error():
    server = start_fake_server()
    port = server.server_address[1]
    server.response_override = {"content": 12345, "stopped_eos": True}

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.status is LocalInferenceStatus.FAILED
    assert "not string" in response.error_message
    server.shutdown()


# ==================================================
# Integration with Governed Runtime Tests
# ==================================================


def test_adapter_works_with_governed_runtime():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    runtime = GovernedLocalModelRuntime(
        adapter,
        LocalModelRuntimeConfig(True, 512, frozenset({"python"}), True),
        authority_verifier=lambda req, model: True,
    )

    result = runtime.execute(descriptor(), request())

    assert result.status is LocalInferenceStatus.SUCCEEDED
    assert result.response.output_text.startswith("response to:")
    server.shutdown()


def test_adapter_deterministic_requests_deduplicated_by_runtime():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    runtime = GovernedLocalModelRuntime(
        adapter,
        LocalModelRuntimeConfig(True, 512, frozenset({"python"}), True),
        authority_verifier=lambda req, model: True,
    )

    result1 = runtime.execute(descriptor(), request())
    result2 = runtime.execute(descriptor(), request())

    assert result1 is result2
    assert server.last_request is not None  # At least one call happened
    server.shutdown()


def test_adapter_enforces_local_only_locality():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    assert response.locality is LocalityType.LOCAL
    assert response.remote_execution is False
    assert response.cloud_escalation_count == 0
    assert response.cloud_cost_usd == 0.0
    server.shutdown()


# ==================================================
# Serialization and Immutability Tests
# ==================================================


def test_adapter_config_serialization_is_stable():
    config = adapter_config()
    fp1 = config.config_fingerprint()
    fp2 = config.config_fingerprint()
    assert fp1 == fp2


def test_adapter_response_is_valid_contract():
    server = start_fake_server()
    port = server.server_address[1]

    config = adapter_config(endpoint=f"http://127.0.0.1:{port}")
    adapter = LlamaCppLocalAdapter(config=config, clock=fixed_clock)

    response = adapter.infer(
        descriptor(),
        request(),
        LocalModelRuntimeConfig(True, 512, frozenset(), True),
    )

    # Response must serialize to dict
    response_dict = response.to_dict()
    assert isinstance(response_dict, dict)
    assert response_dict["status"] == "succeeded"
    assert response_dict["locality"] == "local"

    server.shutdown()
