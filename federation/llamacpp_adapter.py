"""Governed llama.cpp local adapter — loopback only, no arbitrary commands."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, StrEnum
from typing import Any, Mapping
from urllib.parse import urlparse

import urllib3

from tools.ai_controller.local_capability_baseline import LocalityType
from tools.ai_controller.local_model_runtime import (
    AdapterInferenceResponse,
    GenerationConfig,
    LocalInferenceStatus,
    LocalInferenceUsage,
    LocalModelAdapterError,
    LocalModelDescriptor,
    LocalInferenceRequest,
    LocalModelRuntimeConfig,
)


class LlamaCppAdapterMode(StrEnum):
    """Immutable adapter mode for llama.cpp inference endpoint selection."""

    COMPLETION = "completion"
    CHAT = "chat"


class LlamaCppAdapterError(LocalModelAdapterError):
    """Base llama.cpp adapter error."""


class LlamaCppNetworkPolicyError(LlamaCppAdapterError):
    """Endpoint violates loopback-only network restriction."""


class LlamaCppServerIdentityError(LlamaCppAdapterError):
    """Server or model identity mismatch."""


class LlamaCppUnsupportedParameterError(LlamaCppAdapterError):
    """Generation parameter is not supported or cannot be mapped."""


@dataclass(frozen=True, slots=True)
class LlamaCppAdapterConfig:
    """Immutable configuration for governed llama.cpp local adapter."""

    adapter_id: str
    node_id: str
    endpoint: str
    expected_runtime_id: str
    expected_model_id: str
    model_fingerprint: str | None
    connection_timeout_seconds: float
    request_timeout_seconds: float
    maximum_response_bytes: int
    mode: LlamaCppAdapterMode

    def __post_init__(self):
        from tools.ai_controller.local_model_runtime import (
            _identifier,
            _digest,
            _finite_nonnegative,
        )

        _identifier(self.adapter_id, "adapter_id")
        _identifier(self.node_id, "node_id")
        _identifier(self.expected_runtime_id, "expected_runtime_id")
        _identifier(self.expected_model_id, "expected_model_id")

        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise ValueError("endpoint must be non-empty string")
        object.__setattr__(self, "endpoint", self.endpoint.strip())

        if self.model_fingerprint is not None:
            _digest(self.model_fingerprint, "model_fingerprint")

        object.__setattr__(
            self,
            "connection_timeout_seconds",
            _finite_nonnegative(self.connection_timeout_seconds, "connection_timeout_seconds"),
        )
        object.__setattr__(
            self,
            "request_timeout_seconds",
            _finite_nonnegative(self.request_timeout_seconds, "request_timeout_seconds"),
        )

        if (
            not isinstance(self.maximum_response_bytes, int)
            or isinstance(self.maximum_response_bytes, bool)
            or self.maximum_response_bytes < 1
        ):
            raise ValueError("maximum_response_bytes must be positive integer")

        try:
            object.__setattr__(self, "mode", LlamaCppAdapterMode(self.mode))
        except (TypeError, ValueError) as exc:
            raise ValueError("mode must be completion or chat") from exc

        # Validate loopback-only
        _validate_loopback_only(self.endpoint)

    def config_fingerprint(self) -> str:
        """Return stable configuration fingerprint."""
        from tools.ai_controller.local_model_runtime import _fingerprint

        return _fingerprint(
            {
                "adapter_id": self.adapter_id,
                "node_id": self.node_id,
                "endpoint": self.endpoint,
                "expected_runtime_id": self.expected_runtime_id,
                "expected_model_id": self.expected_model_id,
                "model_fingerprint": self.model_fingerprint,
                "connection_timeout_seconds": self.connection_timeout_seconds,
                "request_timeout_seconds": self.request_timeout_seconds,
                "maximum_response_bytes": self.maximum_response_bytes,
                "mode": self.mode.value,
            }
        )


def _validate_loopback_only(endpoint: str) -> None:
    """Validate that endpoint resolves to loopback addresses only."""
    # Handle bare IPv6 addresses by wrapping in brackets
    if "://" not in endpoint and ":" in endpoint and "[" not in endpoint:
        # Might be IPv6, try to parse it
        try:
            addr = ipaddress.ip_address(endpoint)
            if addr.is_loopback:
                return
            raise LlamaCppNetworkPolicyError(
                f"endpoint {endpoint} is not loopback"
            )
        except ValueError:
            # Not a valid IPv6, continue with URL parsing
            pass

    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    hostname = parsed.hostname

    if not hostname:
        raise LlamaCppNetworkPolicyError("endpoint must contain a valid hostname")

    # Normalize localhost variants
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        return

    # Try to parse as IP address
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_loopback:
            return
        raise LlamaCppNetworkPolicyError(
            f"endpoint {hostname} is not loopback (resolved to {addr})"
        )
    except ValueError:
        # Not an IP address, try DNS resolution
        pass

    # Resolve hostname and check all addresses are loopback
    try:
        addr_info = socket.getaddrinfo(hostname, None, family=socket.AF_UNSPEC)
    except socket.gaierror as exc:
        raise LlamaCppNetworkPolicyError(
            f"endpoint {hostname} cannot be resolved"
        ) from exc

    if not addr_info:
        raise LlamaCppNetworkPolicyError(f"endpoint {hostname} has no addresses")

    for family, socktype, proto, canonname, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
            if not addr.is_loopback:
                raise LlamaCppNetworkPolicyError(
                    f"endpoint {hostname} resolves to non-loopback address {ip_str}"
                )
        except ValueError as exc:
            raise LlamaCppNetworkPolicyError(
                f"endpoint {hostname} has invalid address {ip_str}"
            ) from exc


class LlamaCppLocalAdapter:
    """Governed local llama.cpp adapter — loopback HTTP only, no subprocesses."""

    def __init__(
        self,
        *,
        config: LlamaCppAdapterConfig,
        clock=None,
    ):
        if type(config) is not LlamaCppAdapterConfig:
            raise TypeError("config must be LlamaCppAdapterConfig")

        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")

        self._config = config
        self._config_fingerprint = config.config_fingerprint()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

        # Create HTTP pool manager with no proxy and strict timeouts
        self._http = urllib3.PoolManager(
            timeout=urllib3.Timeout(
                connect=config.connection_timeout_seconds,
                read=config.request_timeout_seconds,
            ),
            retries=False,
            maxsize=1,
            block=True,
        )

        # Verify configuration immutability
        if config.config_fingerprint() != self._config_fingerprint:
            raise LlamaCppAdapterError("adapter configuration mutated during initialization")

    def infer(
        self,
        model: LocalModelDescriptor,
        inference_request: LocalInferenceRequest,
        runtime_config: LocalModelRuntimeConfig,
    ) -> AdapterInferenceResponse:
        """Execute local inference through already-running llama-server."""
        with self._lock:
            # Verify configuration immutability
            if self._config.config_fingerprint() != self._config_fingerprint:
                raise LlamaCppAdapterError("adapter configuration mutated")

            # Validate model binding
            if model.runtime_id != self._config.expected_runtime_id:
                raise LlamaCppServerIdentityError(
                    f"model runtime_id {model.runtime_id} does not match "
                    f"expected {self._config.expected_runtime_id}"
                )

            if model.model_id != self._config.expected_model_id:
                raise LlamaCppServerIdentityError(
                    f"model model_id {model.model_id} does not match "
                    f"expected {self._config.expected_model_id}"
                )

            if model.node_id != self._config.node_id:
                raise LlamaCppAdapterError(
                    f"model node_id {model.node_id} does not match adapter {self._config.node_id}"
                )

            # Attest server/model identity
            self._attest_server_model(model)
            if self._config.mode is LlamaCppAdapterMode.CHAT:
                self._attest_chat_capability()

            # Map generation config to llama.cpp request
            llama_request = self._map_request(inference_request)

            # Execute HTTP request
            started_at = self._clock()
            try:
                response_data = self._execute_http_request(llama_request)
            except Exception as exc:
                completed_at = self._clock()
                elapsed = (completed_at - started_at).total_seconds()
                return self._error_response(
                    model,
                    started_at,
                    completed_at,
                    elapsed,
                    "http_request_failed",
                    str(exc),
                )

            completed_at = self._clock()
            elapsed = (completed_at - started_at).total_seconds()

            # Validate and map response
            return self._map_response(
                model,
                inference_request,
                runtime_config,
                response_data,
                started_at,
                completed_at,
                elapsed,
            )

    def _attest_server_model(self, model: LocalModelDescriptor) -> None:
        """Verify server and model identity before inference.

        Queries GET /v1/models to attest that the loaded model matches
        the expected model identity before allowing inference.

        Fails closed on:
        - Malformed metadata response
        - No model loaded/available
        - Model identity conflict
        - Ambiguous model authority
        - Metadata from foreign authority
        """
        # Query /v1/models endpoint to get loaded model metadata
        url = self._config.endpoint.rstrip("/") + "/v1/models"

        try:
            resp = self._http.request(
                "GET",
                url,
                preload_content=False,
            )
        except urllib3.exceptions.HTTPError as exc:
            raise LlamaCppServerIdentityError(
                f"failed to query model metadata: {exc}"
            ) from exc

        # Read bounded response
        try:
            raw_data = resp.read(self._config.maximum_response_bytes + 1)
        except Exception as exc:
            raise LlamaCppServerIdentityError(
                f"failed to read metadata response: {exc}"
            ) from exc
        finally:
            resp.release_conn()

        if len(raw_data) > self._config.maximum_response_bytes:
            raise LlamaCppServerIdentityError(
                f"metadata response exceeds maximum size {self._config.maximum_response_bytes}"
            )

        # Check status
        if resp.status != 200:
            raise LlamaCppServerIdentityError(
                f"llama-server metadata endpoint returned HTTP {resp.status}"
            )

        # Parse JSON
        try:
            metadata = json.loads(raw_data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LlamaCppServerIdentityError(
                f"failed to parse metadata JSON: {exc}"
            ) from exc

        # Validate metadata structure
        if not isinstance(metadata, dict):
            raise LlamaCppServerIdentityError(
                "metadata response is not a JSON object"
            )

        # Extract models list (OpenAI-compatible format)
        models_data = metadata.get("data")
        if models_data is None:
            raise LlamaCppServerIdentityError(
                "metadata response missing 'data' field"
            )

        if not isinstance(models_data, list):
            raise LlamaCppServerIdentityError(
                "metadata 'data' field is not a list"
            )

        if len(models_data) == 0:
            raise LlamaCppServerIdentityError(
                "no models available on server"
            )

        if len(models_data) > 1:
            raise LlamaCppServerIdentityError(
                f"ambiguous model authority: server reports {len(models_data)} models"
            )

        # Extract single model
        model_info = models_data[0]
        if not isinstance(model_info, dict):
            raise LlamaCppServerIdentityError(
                "model info is not a JSON object"
            )

        # Extract model ID
        loaded_model_id = model_info.get("id")
        if loaded_model_id is None:
            raise LlamaCppServerIdentityError(
                "model info missing 'id' field"
            )

        if not isinstance(loaded_model_id, str):
            raise LlamaCppServerIdentityError(
                f"model 'id' is not string: {type(loaded_model_id).__name__}"
            )

        # Verify loaded model matches expected model
        if loaded_model_id != self._config.expected_model_id:
            raise LlamaCppServerIdentityError(
                f"loaded model '{loaded_model_id}' does not match "
                f"expected '{self._config.expected_model_id}'"
            )

    def _attest_chat_capability(self) -> None:
        """Require authoritative server chat-template metadata in chat mode."""
        url = self._config.endpoint.rstrip("/") + "/props"
        try:
            resp = self._http.request("GET", url, preload_content=False)
        except urllib3.exceptions.HTTPError as exc:
            raise LlamaCppServerIdentityError(
                f"failed to query chat template metadata: {exc}") from exc
        try:
            raw_data = resp.read(self._config.maximum_response_bytes + 1)
        except Exception as exc:
            raise LlamaCppServerIdentityError(
                f"failed to read chat template metadata: {exc}") from exc
        finally:
            resp.release_conn()
        if len(raw_data) > self._config.maximum_response_bytes:
            raise LlamaCppServerIdentityError(
                "chat template metadata exceeds maximum response size")
        if resp.status != 200:
            raise LlamaCppServerIdentityError(
                f"chat template metadata endpoint returned HTTP {resp.status}")
        try:
            props = json.loads(raw_data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LlamaCppServerIdentityError(
                f"failed to parse chat template metadata JSON: {exc}") from exc
        if not isinstance(props, dict):
            raise LlamaCppServerIdentityError("chat template metadata is not an object")
        template = props.get("chat_template")
        if not isinstance(template, str) or not template.strip():
            raise LlamaCppServerIdentityError(
                "chat template is missing or not authoritatively available")
        capabilities = props.get("chat_template_caps")
        if capabilities is not None and not isinstance(capabilities, dict):
            raise LlamaCppServerIdentityError("chat template capabilities are malformed")

    def _map_request(
        self, inference_request: LocalInferenceRequest
    ) -> dict[str, Any]:
        """Map LocalInferenceRequest to llama.cpp completion request."""
        gen = inference_request.generation

        if self._config.mode is LlamaCppAdapterMode.CHAT:
            llama_req: dict[str, Any] = {
                "model": self._config.expected_model_id,
                "messages": [{"role": "user", "content": inference_request.input_text}],
                "stream": False,
            }
        else:
            llama_req = {"prompt": inference_request.input_text, "stream": False}

        # Map generation parameters
        if gen.maximum_output_tokens is not None:
            llama_req[
                "max_tokens" if self._config.mode is LlamaCppAdapterMode.CHAT else "n_predict"
            ] = gen.maximum_output_tokens

        if gen.temperature is not None:
            llama_req["temperature"] = gen.temperature

        if gen.seed is not None:
            llama_req["seed"] = gen.seed

        if gen.stop_conditions:
            llama_req["stop"] = list(gen.stop_conditions)

        # Fail closed on structured output requirement
        if gen.structured_output_required:
            raise LlamaCppUnsupportedParameterError(
                "structured_output is not yet supported by llama.cpp adapter v0.1"
            )

        return llama_req

    def _execute_http_request(self, llama_request: dict[str, Any]) -> dict[str, Any]:
        """Execute HTTP POST to llama-server with hardened bounds."""
        route = ("/v1/chat/completions"
                 if self._config.mode is LlamaCppAdapterMode.CHAT else "/completion")
        url = self._config.endpoint.rstrip("/") + route

        # Encode request
        try:
            body = json.dumps(llama_request, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise LlamaCppAdapterError(
                "failed to serialize llama.cpp request"
            ) from exc

        # Execute POST request
        try:
            resp = self._http.request(
                "POST",
                url,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                preload_content=False,
            )
        except urllib3.exceptions.HTTPError as exc:
            raise LlamaCppAdapterError(f"HTTP request failed: {exc}") from exc

        # Read bounded response
        try:
            raw_data = resp.read(self._config.maximum_response_bytes + 1)
        except Exception as exc:
            raise LlamaCppAdapterError(f"failed to read response body: {exc}") from exc
        finally:
            resp.release_conn()

        if len(raw_data) > self._config.maximum_response_bytes:
            raise LlamaCppAdapterError(
                f"response exceeds maximum size {self._config.maximum_response_bytes}"
            )

        # Check status
        if resp.status != 200:
            raise LlamaCppAdapterError(
                f"llama-server returned HTTP {resp.status}: {raw_data[:200]}"
            )

        # Parse JSON
        try:
            return json.loads(raw_data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LlamaCppAdapterError(f"failed to parse JSON response: {exc}") from exc

    def _map_response(
        self,
        model: LocalModelDescriptor,
        inference_request: LocalInferenceRequest,
        runtime_config: LocalModelRuntimeConfig,
        response_data: dict[str, Any],
        started_at: datetime,
        completed_at: datetime,
        elapsed_seconds: float,
    ) -> AdapterInferenceResponse:
        """Map llama.cpp response to AdapterInferenceResponse."""
        if self._config.mode is LlamaCppAdapterMode.CHAT:
            return self._map_chat_response(
                model, response_data, started_at, completed_at, elapsed_seconds)

        # Extract generated text
        output_text = response_data.get("content")
        if output_text is None:
            return self._error_response(
                model,
                started_at,
                completed_at,
                elapsed_seconds,
                "missing_output",
                "response missing 'content' field",
            )

        if not isinstance(output_text, str):
            return self._error_response(
                model,
                started_at,
                completed_at,
                elapsed_seconds,
                "invalid_output_type",
                f"content field is not string: {type(output_text)}",
            )

        termination_reason, termination_error = self._completion_termination(response_data)
        if termination_error is not None:
            return self._error_response(model, started_at, completed_at, elapsed_seconds,
                "malformed_termination_metadata", termination_error)

        # Extract token counts - fail closed on malformed
        timings = response_data.get("timings", {})
        prompt_tokens = timings.get("prompt_n")
        generated_tokens = timings.get("predicted_n")

        # Validate prompt_tokens - fail closed on malformed
        if prompt_tokens is not None:
            # Reject booleans (must check before int since bool is subclass of int)
            if isinstance(prompt_tokens, bool):
                return self._error_response(
                    model,
                    started_at,
                    completed_at,
                    elapsed_seconds,
                    "malformed_token_count",
                    f"prompt_n is boolean: {prompt_tokens}",
                )
            # Reject non-integers (strings, floats, lists, dicts, etc.)
            if not isinstance(prompt_tokens, int):
                return self._error_response(
                    model,
                    started_at,
                    completed_at,
                    elapsed_seconds,
                    "malformed_token_count",
                    f"prompt_n is not integer: {type(prompt_tokens).__name__}",
                )
            # Reject negative
            if prompt_tokens < 0:
                return self._error_response(
                    model,
                    started_at,
                    completed_at,
                    elapsed_seconds,
                    "malformed_token_count",
                    f"prompt_n is negative: {prompt_tokens}",
                )

        # Validate generated_tokens - fail closed on malformed
        if generated_tokens is not None:
            # Reject booleans (must check before int since bool is subclass of int)
            if isinstance(generated_tokens, bool):
                return self._error_response(
                    model,
                    started_at,
                    completed_at,
                    elapsed_seconds,
                    "malformed_token_count",
                    f"predicted_n is boolean: {generated_tokens}",
                )
            # Reject non-integers (strings, floats, lists, dicts, etc.)
            if not isinstance(generated_tokens, int):
                return self._error_response(
                    model,
                    started_at,
                    completed_at,
                    elapsed_seconds,
                    "malformed_token_count",
                    f"predicted_n is not integer: {type(generated_tokens).__name__}",
                )
            # Reject negative
            if generated_tokens < 0:
                return self._error_response(
                    model,
                    started_at,
                    completed_at,
                    elapsed_seconds,
                    "malformed_token_count",
                    f"predicted_n is negative: {generated_tokens}",
                )

        # Extract timing measurements if available
        resource_measurements: dict[str, Any] = {}
        if isinstance(timings, dict):
            prompt_ms = timings.get("prompt_ms")
            predicted_ms = timings.get("predicted_ms")
            if isinstance(prompt_ms, (int, float)) and math.isfinite(prompt_ms) and prompt_ms >= 0:
                resource_measurements["prompt_ms"] = float(prompt_ms)
            if isinstance(predicted_ms, (int, float)) and math.isfinite(predicted_ms) and predicted_ms >= 0:
                resource_measurements["predicted_ms"] = float(predicted_ms)

        # Build successful response
        return AdapterInferenceResponse(
            status=LocalInferenceStatus.SUCCEEDED,
            model_id=model.model_id,
            runtime_id=model.runtime_id,
            node_id=model.node_id,
            locality=LocalityType.LOCAL,
            remote_execution=False,
            output_text=output_text,
            structured_output=None,
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            elapsed_seconds=elapsed_seconds,
            usage=LocalInferenceUsage(
                prompt_tokens=prompt_tokens,
                generated_tokens=generated_tokens,
            ),
            resource_measurements=resource_measurements,
            termination_reason=termination_reason,
            error_code=None,
            error_message=None,
            cloud_escalation_count=0,
            cloud_cost_usd=0.0,
        )

    @staticmethod
    def _completion_termination(response_data):
        if "stop_type" in response_data:
            stop_type = response_data["stop_type"]
            if not isinstance(stop_type, str) or stop_type not in {
                    "limit", "eos", "word", "none"}:
                return None, "stop_type is malformed or unsupported"
            return {"limit": "max_tokens", "eos": "eos",
                    "word": "stop_word", "none": "unknown"}[stop_type], None
        present = {name: response_data[name] for name in (
            "stopped_word", "stopped_limit", "stopped_eos") if name in response_data}
        if any(not isinstance(value, bool) for value in present.values()):
            return None, "legacy termination flags must be boolean"
        active = [name for name, value in present.items() if value]
        if len(active) > 1:
            return None, "legacy termination flags conflict"
        if not active:
            return "unknown", None
        return {"stopped_word": "stop_word", "stopped_limit": "max_tokens",
                "stopped_eos": "eos"}[active[0]], None

    def _map_chat_response(self, model, response_data, started_at, completed_at,
                           elapsed_seconds):
        def failed(code, message):
            return self._error_response(model, started_at, completed_at,
                elapsed_seconds, code, message)

        if not isinstance(response_data, dict):
            return failed("malformed_chat_response", "chat response is not an object")
        if response_data.get("model") != self._config.expected_model_id:
            return failed("model_identity_mismatch", "chat response model identity mismatch")
        choices = response_data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            return failed("malformed_chat_response", "chat response must contain one choice")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index") != 0:
            return failed("malformed_chat_response", "chat choice identity is malformed")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return failed("malformed_chat_response", "chat choice is not an assistant message")
        content = message.get("content")
        if not isinstance(content, str):
            return failed("malformed_chat_response", "assistant content is not text")
        finish_reason = choice.get("finish_reason")
        if finish_reason is None:
            termination_reason = "unknown"
        elif not isinstance(finish_reason, str) or finish_reason not in {"stop", "length"}:
            return failed("malformed_termination_metadata",
                "chat finish_reason is malformed or unsupported")
        else:
            termination_reason = {"stop": "eos_or_stop",
                                  "length": "max_tokens"}[finish_reason]

        prompt_tokens = generated_tokens = None
        usage = response_data.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                return failed("malformed_token_count", "chat usage is not an object")
            prompt_tokens = usage.get("prompt_tokens")
            generated_tokens = usage.get("completion_tokens")
            for name, value in (("prompt_tokens", prompt_tokens),
                                ("completion_tokens", generated_tokens)):
                if value is not None and (not isinstance(value, int)
                                          or isinstance(value, bool) or value < 0):
                    return failed("malformed_token_count", f"{name} is malformed")
            total = usage.get("total_tokens")
            if total is not None and (not isinstance(total, int) or isinstance(total, bool)
                                      or total < 0):
                return failed("malformed_token_count", "total_tokens is malformed")
            if (total is not None and prompt_tokens is not None
                    and generated_tokens is not None
                    and total != prompt_tokens + generated_tokens):
                return failed("malformed_token_count", "total_tokens is contradictory")

        resources = {}
        timings = response_data.get("timings")
        if timings is not None:
            if not isinstance(timings, dict):
                return failed("malformed_timing_metadata", "timings is not an object")
            for source, current, label in (
                    ("prompt_n", prompt_tokens, "prompt_tokens"),
                    ("predicted_n", generated_tokens, "completion_tokens")):
                value = timings.get(source)
                if value is not None and (not isinstance(value, int)
                                          or isinstance(value, bool) or value < 0):
                    return failed("malformed_token_count", f"timings {source} is malformed")
                if current is not None and value is not None and current != value:
                    return failed("malformed_token_count",
                        f"usage {label} conflicts with timings {source}")
                if source == "prompt_n" and prompt_tokens is None:
                    prompt_tokens = value
                if source == "predicted_n" and generated_tokens is None:
                    generated_tokens = value
            for source, target in (("prompt_ms", "prompt_ms"),
                                   ("predicted_ms", "predicted_ms")):
                value = timings.get(source)
                if value is not None:
                    if (not isinstance(value, (int, float)) or isinstance(value, bool)
                            or not math.isfinite(value) or value < 0):
                        return failed("malformed_timing_metadata", f"{source} is malformed")
                    resources[target] = float(value)

        return AdapterInferenceResponse(
            status=LocalInferenceStatus.SUCCEEDED, model_id=model.model_id,
            runtime_id=model.runtime_id, node_id=model.node_id,
            locality=LocalityType.LOCAL, remote_execution=False,
            output_text=content, structured_output=None,
            started_at=started_at.isoformat(), completed_at=completed_at.isoformat(),
            elapsed_seconds=elapsed_seconds,
            usage=LocalInferenceUsage(prompt_tokens, generated_tokens),
            resource_measurements=resources, termination_reason=termination_reason,
            error_code=None, error_message=None, cloud_escalation_count=0,
            cloud_cost_usd=0.0)

    def _error_response(
        self,
        model: LocalModelDescriptor,
        started_at: datetime,
        completed_at: datetime,
        elapsed_seconds: float,
        error_code: str,
        error_message: str,
    ) -> AdapterInferenceResponse:
        """Build error response."""
        return AdapterInferenceResponse(
            status=LocalInferenceStatus.FAILED,
            model_id=model.model_id,
            runtime_id=model.runtime_id,
            node_id=model.node_id,
            locality=LocalityType.LOCAL,
            remote_execution=False,
            output_text=None,
            structured_output=None,
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            elapsed_seconds=elapsed_seconds,
            usage=LocalInferenceUsage(
                prompt_tokens=None,
                generated_tokens=None,
            ),
            resource_measurements={},
            termination_reason=error_code,
            error_code=error_code,
            error_message=error_message,
            cloud_escalation_count=0,
            cloud_cost_usd=0.0,
        )
