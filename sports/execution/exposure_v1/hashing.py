from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import hmac
import json
import secrets
from types import MappingProxyType
from typing import Any

_AUTHORITY_SEAL_KEY = secrets.token_bytes(32)


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_normalize(item) for item in value]
    if isinstance(value, set):
        return sorted(_normalize(item) for item in value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, MappingProxyType):
        normalized = {str(k): _normalize(v) for k, v in dict(value).items()}
        return dict(sorted(normalized.items(), key=lambda item: item[0]))
    if isinstance(value, dict):
        normalized = {str(k): _normalize(v) for k, v in value.items()}
        return dict(sorted(normalized.items(), key=lambda item: item[0]))
    if is_dataclass(value):
        return {
            field_spec.name: _normalize(getattr(value, field_spec.name))
            for field_spec in fields(value)
            if not field_spec.name.startswith("_")
        }
    return value


def canonical_json(payload: Any) -> str:
    return json.dumps(
        _normalize(payload),
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    )


def stable_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _seal_authoritative_hash(canonical_hash: str) -> str:
    if not isinstance(canonical_hash, str) or not canonical_hash:
        raise ValueError("canonical_hash must be a non-empty string")
    return hmac.new(
        _AUTHORITY_SEAL_KEY,
        canonical_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _authoritative_hash_matches_seal(
    canonical_hash: str,
    seal: str,
) -> bool:
    if (
        not isinstance(canonical_hash, str)
        or not canonical_hash
        or not isinstance(seal, str)
        or not seal
    ):
        return False
    return hmac.compare_digest(_seal_authoritative_hash(canonical_hash), seal)
