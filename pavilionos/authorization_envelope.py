"""Authorization envelope for secure Pavilion provider adapter invocation.

The authorization envelope carries all required canonical lineage identifiers
plus the transient permit token required to authorize provider execution.

SECURITY CRITICAL:
- The permit_token field contains transient secret authorization material
- It MUST NOT be persisted to any durable storage
- It MUST NOT be logged or included in receipts
- It MUST NOT appear in process argv or environment
- It travels ONLY via subprocess stdin pipe
- It is consumed exactly once by verify_and_consume_permit()
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from federation.control_domain import validate_domain_id


def _require_text(value: Any, field: str, *, max_length: int = 255) -> str:
    """Require non-empty trimmed string within length limit."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field} must not have surrounding whitespace")
    if len(value) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NULL bytes")
    return value


def _require_hex_64(value: Any, field: str) -> str:
    """Require exactly 64 lowercase hex characters (SHA-256)."""
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field} must be 64-character hex string")
    if not all(c in "0123456789abcdef" for c in value):
        raise ValueError(f"{field} must be lowercase hex")
    return value


def _canonical_json(value: Any) -> bytes:
    """Canonical JSON serialization for fingerprinting."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(slots=True, frozen=True)
class PavilionAuthorizationEnvelope:
    """Secure authorization envelope for Pavilion provider adapter.

    Contains all canonical lineage identifiers required to bind the
    provider execution to exact canonical authority, plus the transient
    permit token that authorizes a single provider handoff.

    The envelope is transferred from coordinator to adapter via subprocess
    stdin as JSON. The adapter MUST consume the permit via the gateway
    before any provider operation may execute.
    """

    # Control and scope
    control_domain: str
    provider_id: str
    adapter_id: str
    action: str

    # Canonical lineage
    effect_intent_id: str
    effect_dispatch_id: str
    authority_reservation_id: str
    gateway_claim_id: str

    # Delegation and capability
    delegation_grant_id: str
    delegation_grant_fingerprint: str
    requested_capability: str

    # Operation identity
    operation_digest: str
    idempotency_key: str

    # Credential scope
    credential_scope: tuple[str, ...]

    # Transient secret authorization (NOT persisted)
    permit_token: str

    def __post_init__(self) -> None:
        """Validate envelope invariants."""
        # Validate control domain
        object.__setattr__(
            self,
            "control_domain",
            validate_domain_id(self.control_domain, "control_domain"),
        )

        # Validate text fields
        for field_name in (
            "provider_id",
            "adapter_id",
            "action",
            "effect_intent_id",
            "effect_dispatch_id",
            "authority_reservation_id",
            "gateway_claim_id",
            "delegation_grant_id",
            "requested_capability",
            "idempotency_key",
            "permit_token",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_text(getattr(self, field_name), field_name),
            )

        # Validate delegation fingerprint (64-char hex)
        object.__setattr__(
            self,
            "delegation_grant_fingerprint",
            _require_hex_64(
                self.delegation_grant_fingerprint,
                "delegation_grant_fingerprint",
            ),
        )

        # Validate operation digest (64-char hex SHA-256)
        object.__setattr__(
            self,
            "operation_digest",
            _require_hex_64(self.operation_digest, "operation_digest"),
        )

        # Normalize credential scope
        if not isinstance(self.credential_scope, tuple):
            if isinstance(self.credential_scope, str):
                raise TypeError("credential_scope must be tuple of strings")
            object.__setattr__(
                self,
                "credential_scope",
                tuple(self.credential_scope),
            )

        validated_scopes = []
        for scope in self.credential_scope:
            validated_scopes.append(_require_text(scope, "credential scope"))
        if len(validated_scopes) != len(set(validated_scopes)):
            raise ValueError("credential_scope must not contain duplicates")
        object.__setattr__(
            self,
            "credential_scope",
            tuple(sorted(validated_scopes)),
        )

    def to_json(self) -> str:
        """Serialize envelope to JSON for stdin transfer.

        WARNING: The result contains the transient permit_token secret.
        NEVER persist this to files, logs, or durable storage.
        NEVER pass via argv or environment.
        ONLY transmit via subprocess stdin pipe.
        """
        payload = {
            "control_domain": self.control_domain,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "action": self.action,
            "effect_intent_id": self.effect_intent_id,
            "effect_dispatch_id": self.effect_dispatch_id,
            "authority_reservation_id": self.authority_reservation_id,
            "gateway_claim_id": self.gateway_claim_id,
            "delegation_grant_id": self.delegation_grant_id,
            "delegation_grant_fingerprint": self.delegation_grant_fingerprint,
            "requested_capability": self.requested_capability,
            "operation_digest": self.operation_digest,
            "idempotency_key": self.idempotency_key,
            "credential_scope": list(self.credential_scope),
            "permit_token": self.permit_token,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, json_str: str) -> "PavilionAuthorizationEnvelope":
        """Parse envelope from JSON received via stdin."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON in authorization envelope") from exc

        if not isinstance(data, dict):
            raise ValueError("Authorization envelope must be a JSON object")

        # Extract required fields
        try:
            return cls(
                control_domain=data["control_domain"],
                provider_id=data["provider_id"],
                adapter_id=data["adapter_id"],
                action=data["action"],
                effect_intent_id=data["effect_intent_id"],
                effect_dispatch_id=data["effect_dispatch_id"],
                authority_reservation_id=data["authority_reservation_id"],
                gateway_claim_id=data["gateway_claim_id"],
                delegation_grant_id=data["delegation_grant_id"],
                delegation_grant_fingerprint=data["delegation_grant_fingerprint"],
                requested_capability=data["requested_capability"],
                operation_digest=data["operation_digest"],
                idempotency_key=data["idempotency_key"],
                credential_scope=tuple(data.get("credential_scope", ())),
                permit_token=data["permit_token"],
            )
        except KeyError as exc:
            raise ValueError(f"Missing required field in envelope: {exc}") from exc

    def envelope_fingerprint(self) -> str:
        """Compute stable fingerprint of envelope (excluding permit_token)."""
        payload = {
            "control_domain": self.control_domain,
            "provider_id": self.provider_id,
            "adapter_id": self.adapter_id,
            "action": self.action,
            "effect_intent_id": self.effect_intent_id,
            "effect_dispatch_id": self.effect_dispatch_id,
            "authority_reservation_id": self.authority_reservation_id,
            "gateway_claim_id": self.gateway_claim_id,
            "delegation_grant_id": self.delegation_grant_id,
            "delegation_grant_fingerprint": self.delegation_grant_fingerprint,
            "requested_capability": self.requested_capability,
            "operation_digest": self.operation_digest,
            "idempotency_key": self.idempotency_key,
            "credential_scope": list(self.credential_scope),
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def __repr__(self) -> str:
        """Safe representation that does NOT include permit_token."""
        return (
            f"PavilionAuthorizationEnvelope("
            f"control_domain={self.control_domain!r}, "
            f"action={self.action!r}, "
            f"gateway_claim_id={self.gateway_claim_id!r}, "
            f"effect_intent_id={self.effect_intent_id!r})"
        )


__all__ = ["PavilionAuthorizationEnvelope"]
