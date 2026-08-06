"""Shared keyed-authenticity primitives for durable federation evidence."""

import hashlib
import hmac


MINIMUM_INTEGRITY_KEY_BYTES = 32


def require_integrity_key(value: bytes) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError("integrity_key must be bytes")
    if len(value) < MINIMUM_INTEGRITY_KEY_BYTES:
        raise ValueError(
            f"integrity_key must contain at least {MINIMUM_INTEGRITY_KEY_BYTES} bytes",
        )
    return value


def authentication_tag(key: bytes, domain: bytes, canonical_record: bytes) -> str:
    return hmac.new(
        key,
        domain + b"\0" + canonical_record,
        hashlib.sha256,
    ).hexdigest()


def authenticates(
    key: bytes,
    domain: bytes,
    canonical_record: bytes,
    claimed_tag: str,
) -> bool:
    if not isinstance(claimed_tag, str):
        return False
    expected = authentication_tag(key, domain, canonical_record)
    return hmac.compare_digest(expected, claimed_tag)
