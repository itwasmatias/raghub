"""Narrow in-memory credential release boundary for authorized effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from federation.effect_boundary import EffectAuthorityError, EffectBoundary, EffectRequest
from federation.power_action import PowerStatus


_MAX_TEXT_LENGTH = 255


class CredentialBrokerError(Exception):
    """Base error for credential broker operations."""


class CredentialBrokerAuthorityError(CredentialBrokerError):
    """Raised when effect authority is missing, stale, or revoked."""


class CredentialBrokerNotFoundError(CredentialBrokerError):
    """Raised when a referenced credential cannot be resolved."""


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
    if len(value) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{field_name} exceeds {_MAX_TEXT_LENGTH} characters")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NULL bytes")
    return value


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """Opaque credential identifier bound to a provider and kind."""

    credential_id: str
    provider: str = "default"
    kind: str = "opaque"

    def __post_init__(self) -> None:
        object.__setattr__(self, "credential_id", _require_text(self.credential_id, "credential_id"))
        object.__setattr__(self, "provider", _require_text(self.provider, "provider"))
        object.__setattr__(self, "kind", _require_text(self.kind, "kind"))

    def to_dict(self) -> dict[str, str]:
        return {
            "credential_id": self.credential_id,
            "provider": self.provider,
            "kind": self.kind,
        }

    def lookup_key(self) -> tuple[str, str, str]:
        return self.provider, self.kind, self.credential_id


@dataclass(frozen=True, slots=True)
class CredentialLease:
    """Ephemeral release of secret material for one authorized effect."""

    credential_ref: CredentialRef
    request_id: str
    effect_request_id: str
    domain_id: str
    mission_id: str
    agent_id: str
    grant_id: str
    action: str
    authorization_expiration: datetime | None
    released_at: datetime
    decision: Any = field(repr=False, compare=False)
    _secret: bytes = field(repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_text(self.request_id, "request_id"))
        object.__setattr__(self, "effect_request_id", _require_text(self.effect_request_id, "effect_request_id"))
        object.__setattr__(self, "domain_id", _require_text(self.domain_id, "domain_id"))
        object.__setattr__(self, "mission_id", _require_text(self.mission_id, "mission_id"))
        object.__setattr__(self, "agent_id", _require_text(self.agent_id, "agent_id"))
        object.__setattr__(self, "grant_id", _require_text(self.grant_id, "grant_id"))
        object.__setattr__(self, "action", _require_text(self.action, "action"))
        if self.authorization_expiration is not None:
            if not isinstance(self.authorization_expiration, datetime) or self.authorization_expiration.tzinfo is None:
                raise TypeError("authorization_expiration must be a timezone-aware datetime or None")
            object.__setattr__(
                self,
                "authorization_expiration",
                self.authorization_expiration.astimezone(timezone.utc),
            )
        if not isinstance(self.released_at, datetime) or self.released_at.tzinfo is None:
            raise TypeError("released_at must be a timezone-aware datetime")
        object.__setattr__(self, "released_at", self.released_at.astimezone(timezone.utc))
        if type(self.credential_ref) is not CredentialRef:
            raise TypeError("credential_ref must be a CredentialRef")
        if type(self._secret) is not bytes:
            raise TypeError("secret material must be bytes")
        if not self._secret:
            raise ValueError("secret material must be non-empty")

    def secret_bytes(self) -> bytes:
        return bytes(self._secret)

    def secret_text(self, encoding: str = "utf-8") -> str:
        return self._secret.decode(encoding)

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_ref": self.credential_ref.to_dict(),
            "request_id": self.request_id,
            "effect_request_id": self.effect_request_id,
            "domain_id": self.domain_id,
            "mission_id": self.mission_id,
            "agent_id": self.agent_id,
            "grant_id": self.grant_id,
            "action": self.action,
            "authorization_expiration": (
                None
                if self.authorization_expiration is None
                else self.authorization_expiration.astimezone(timezone.utc).isoformat()
            ),
            "released_at": self.released_at.astimezone(timezone.utc).isoformat(),
            "status": "released",
        }


class CredentialBroker:
    """Release secret material only after the existing effect boundary authorizes it."""

    def __init__(
        self,
        *,
        effect_boundary: EffectBoundary,
        secret_source: Callable[[CredentialRef], Any] | Mapping[str, Any],
    ) -> None:
        if type(effect_boundary) is not EffectBoundary:
            raise TypeError("effect_boundary must be an EffectBoundary")
        self.effect_boundary = effect_boundary
        if isinstance(secret_source, Mapping):
            self._secret_source = lambda ref: secret_source[ref.lookup_key()]
        elif callable(secret_source):
            self._secret_source = secret_source
        else:
            raise TypeError("secret_source must be callable or mapping")

    @staticmethod
    def _released_at() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _secret_bytes(secret: Any, credential_ref: CredentialRef) -> bytes:
        if isinstance(secret, str):
            data = secret.encode("utf-8")
        elif isinstance(secret, (bytes, bytearray, memoryview)):
            data = bytes(secret)
        else:
            raise CredentialBrokerError(
                f"credential provider returned unsupported material for {credential_ref.credential_id!r}",
            )
        if not data:
            raise CredentialBrokerError(
                f"credential provider returned empty material for {credential_ref.credential_id!r}",
            )
        return data

    def _resolve_secret(self, credential_ref: CredentialRef) -> bytes:
        try:
            secret = self._secret_source(credential_ref)
        except KeyError as exc:
            raise CredentialBrokerNotFoundError(
                f"credential {credential_ref.credential_id!r} is not registered",
            ) from exc
        except LookupError as exc:
            raise CredentialBrokerNotFoundError(
                f"credential {credential_ref.credential_id!r} is not registered",
            ) from exc
        return self._secret_bytes(secret, credential_ref)

    def release(self, request: EffectRequest, credential_ref: CredentialRef) -> CredentialLease:
        if type(request) is not EffectRequest:
            raise TypeError("request must be an EffectRequest")
        if type(credential_ref) is not CredentialRef:
            raise TypeError("credential_ref must be a CredentialRef")
        with self.effect_boundary._authority_scope():
            try:
                evidence = self.effect_boundary.inspect(request)
            except EffectAuthorityError as exc:
                raise CredentialBrokerAuthorityError(str(exc)) from exc
            if evidence.request != request:
                raise CredentialBrokerAuthorityError("effect request context changed before credential release")
            if evidence.outcome.snapshot.status is not PowerStatus.EXECUTION_AUTHORIZED:
                raise CredentialBrokerAuthorityError("effect is not execution authorized")
            secret = self._resolve_secret(credential_ref)
            released_at = self._released_at()
            return CredentialLease(
                credential_ref=credential_ref,
                request_id=request.effect_request_id,
                effect_request_id=request.effect_request_id,
                domain_id=request.domain_id,
                mission_id=request.mission_id,
                agent_id=request.agent_id,
                grant_id=request.grant_id,
                action=request.proposal.action.value,
                authorization_expiration=evidence.attempt.authorization_expiration if evidence.attempt is not None else None,
                released_at=released_at,
                decision=evidence.decision,
                _secret=secret,
            )


__all__ = [
    "CredentialBroker",
    "CredentialBrokerAuthorityError",
    "CredentialBrokerError",
    "CredentialBrokerNotFoundError",
    "CredentialLease",
    "CredentialRef",
]
