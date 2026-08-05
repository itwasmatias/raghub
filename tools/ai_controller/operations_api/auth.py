from __future__ import annotations

import hmac
import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import request

from .errors import APIError

ALLOWED_CAPABILITIES = frozenset(
    {
        "controller.read",
        "controller.propose",
        "controller.approve",
        "controller.apply",
    }
)


@dataclass(frozen=True)
class Principal:
    identity: str
    capabilities: frozenset[str]


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("controller token configuration has duplicate keys")
        result[key] = value
    return result


def validate_tokens(
    tokens: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(tokens, dict):
        raise ValueError("controller token configuration must be an object")
    validated: dict[str, dict[str, Any]] = {}
    principals: set[str] = set()
    for token, entry in tokens.items():
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or not isinstance(entry, dict)
            or set(entry) != {"principal", "capabilities"}
        ):
            raise ValueError("controller token entry has an invalid schema")
        principal = entry["principal"]
        capabilities = entry["capabilities"]
        if (
            not isinstance(principal, str)
            or not principal
            or principal != principal.strip()
            or len(principal) > 128
            or principal in principals
            or not isinstance(capabilities, list)
            or not capabilities
            or any(
                not isinstance(capability, str)
                or capability not in ALLOWED_CAPABILITIES
                for capability in capabilities
            )
            or len(set(capabilities)) != len(capabilities)
        ):
            raise ValueError("controller token entry has invalid values")
        principals.add(principal)
        validated[token] = {
            "principal": principal,
            "capabilities": list(capabilities),
        }
    return validated


def load_tokens_from_environment() -> dict[str, dict[str, Any]]:
    raw = os.getenv("RAGHUB_CONTROLLER_TOKENS", "").strip()
    secret_file = os.getenv("RAGHUB_CONTROLLER_TOKEN_FILE", "").strip()
    if raw and secret_file:
        raise ValueError("configure controller tokens from only one source")
    if secret_file:
        raw = Path(secret_file).read_text(encoding="utf-8")
    if not raw:
        return {}
    parsed = json.loads(raw, object_pairs_hook=_strict_object)
    return validate_tokens(parsed)


def authenticate(
    tokens: dict[str, dict[str, Any]],
    required_capability: str,
) -> Principal:
    if "token" in request.args or "access_token" in request.args:
        raise APIError(
            "UNAUTHENTICATED",
            "Bearer authentication is required.",
            401,
        )
    authorization = request.headers.get("Authorization", "")
    supplied = (
        authorization[len("Bearer ") :].strip()
        if authorization.startswith("Bearer ")
        else ""
    )
    matched: dict[str, Any] | None = None
    for expected, configured in tokens.items():
        if hmac.compare_digest(supplied, expected):
            matched = configured
    if not supplied or matched is None:
        raise APIError(
            "UNAUTHENTICATED",
            "Bearer authentication is required.",
            401,
        )
    capabilities = frozenset(matched["capabilities"])
    principal = Principal(
        identity=matched["principal"],
        capabilities=capabilities,
    )
    if required_capability not in capabilities:
        raise APIError(
            "FORBIDDEN",
            "The authenticated principal lacks the required capability.",
            403,
        )
    return principal


def validate_bind_address(
    address: str,
    *,
    tailscale_address: str | None = None,
) -> str:
    if not isinstance(address, str) or not address or address != address.strip():
        raise ValueError("controller bind address must be one exact IP address")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ValueError("controller bind address must be an IP address") from exc
    if parsed.is_loopback:
        return address
    if not tailscale_address:
        raise ValueError(
            "non-loopback controller binding requires a configured Tailscale address"
        )
    if tailscale_address != tailscale_address.strip():
        raise ValueError("configured Tailscale address is malformed")
    try:
        configured = ipaddress.ip_address(tailscale_address)
    except ValueError as exc:
        raise ValueError("configured Tailscale address must be IPv4") from exc
    if (
        not isinstance(configured, ipaddress.IPv4Address)
        or configured not in ipaddress.ip_network("100.64.0.0/10")
    ):
        raise ValueError("configured Tailscale address must be an exact CGNAT IPv4")
    if parsed == configured:
        return address
    raise ValueError(
        "controller API may bind only to loopback or an explicit Tailscale address"
    )
